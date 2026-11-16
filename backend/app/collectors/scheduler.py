"""Schedules the enabled collection jobs inside the backend process.

Jobs and their schedules come from the `collection_job` table, which the
seed file fills. Every schedule is aligned to the clock in UTC:

- `interval_minutes` below 60 fires on the minutes divisible by it (every
  15 minutes fires at :00, :15, :30 and :45);
- whole hours fire on the hours divisible by it, at minute 0;
- `daily_at` fires once a day at that time.

Run one scheduler per deployment. The per-job lock taken by the runner stops
two schedulers from running the same job at once, but each would still
attempt every run and spend its provider budget.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.collectors.runner import (
    JobAlreadyRunningError,
    JobDisabledError,
    JobNotFoundError,
    JobRunner,
    RunHandle,
)
from app.models import CollectionJob
from app.models.enums import CollectorStatus, ExecutionTrigger
from app.repositories.collection_jobs import SqlCollectionJobRepository

logger = logging.getLogger(__name__)

# A run that starts this many seconds late (for example after a pause) still runs.
_MISFIRE_GRACE_SECONDS = 120


def build_trigger(job: CollectionJob) -> BaseTrigger:
    """The APScheduler trigger for a job's schedule, in UTC."""
    if job.daily_at is not None:
        return CronTrigger(hour=job.daily_at.hour, minute=job.daily_at.minute, timezone=UTC)
    minutes = job.interval_minutes
    if minutes is None or minutes <= 0:
        raise ValueError(f"job {job.name} has no schedule")
    if minutes < 60:
        return CronTrigger(minute=f"*/{minutes}", timezone=UTC)
    return CronTrigger(hour=f"*/{minutes // 60}", minute=0, timezone=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CollectorScheduler:
    def __init__(
        self,
        *,
        runner: JobRunner,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._runner = runner
        self._sessions = session_factory
        self._clock = clock
        self._scheduler = AsyncIOScheduler(timezone=UTC)
        self._tasks: set[asyncio.Task[object]] = set()

    async def start(self) -> list[str]:
        """Close interrupted executions and schedule every enabled job; returns their names."""
        await self._runner.mark_interrupted()
        async with self._sessions() as session:
            jobs = await SqlCollectionJobRepository(session).list(enabled=True)

        for job in jobs:
            self._scheduler.add_job(
                self._run_scheduled,
                build_trigger(job),
                args=[job.name, ExecutionTrigger.SCHEDULED],
                id=job.name,
                name=job.name,
                # One run of a job at a time; a run due while one is going is dropped.
                max_instances=1,
                coalesce=True,
                misfire_grace_time=_MISFIRE_GRACE_SECONDS,
            )
        self._scheduler.start()
        for job in jobs:
            await self._save_next_run(job.name)
        logger.info("Scheduler started", extra={"jobs": [job.name for job in jobs]})
        return [job.name for job in jobs]

    async def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("Scheduler stopped")

    def next_run(self, job_name: str) -> datetime | None:
        job = self._scheduler.get_job(job_name)
        return None if job is None else job.next_run_time

    async def trigger_manual(self, job_name: str) -> RunHandle:
        """Start a run now and return without waiting for it to finish.

        Raises JobNotFoundError, JobDisabledError or JobAlreadyRunningError.
        """
        handle = await self._runner.prepare(job_name, ExecutionTrigger.MANUAL)
        assert handle is not None  # manual runs are never skipped
        task = asyncio.create_task(handle.execute(), name=f"manual:{job_name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return handle

    async def _run_scheduled(self, job_name: str, trigger: ExecutionTrigger) -> None:
        try:
            result = await self._runner.run(job_name, trigger)
        except JobAlreadyRunningError:
            logger.warning("Previous run still going; run skipped", extra={"job": job_name})
            return
        except (JobNotFoundError, JobDisabledError):
            logger.warning("Job no longer enabled; unscheduled", extra={"job": job_name})
            if self._scheduler.get_job(job_name) is not None:
                self._scheduler.remove_job(job_name)
            return
        except Exception:
            # Never let one job's failure stop the scheduler.
            logger.exception("Scheduled run could not start", extra={"job": job_name})
            return
        finally:
            await self._save_next_run(job_name)

        if result is not None and result.status is CollectorStatus.FAILED:
            await self._schedule_retry(job_name, trigger)

    async def _schedule_retry(self, job_name: str, trigger: ExecutionTrigger) -> None:
        """Run a failed job once more after its retry delay, unless it runs sooner anyway."""
        if trigger is ExecutionTrigger.RETRY:
            return
        async with self._sessions() as session:
            job = await SqlCollectionJobRepository(session).get(job_name)
        if job is None or job.retry_minutes is None:
            return
        at = self._clock() + timedelta(minutes=job.retry_minutes)
        regular = self.next_run(job_name)
        if regular is not None and regular <= at:
            return
        self._scheduler.add_job(
            self._run_scheduled,
            DateTrigger(run_date=at, timezone=UTC),
            args=[job_name, ExecutionTrigger.RETRY],
            id=f"{job_name}:retry",
            name=f"{job_name} (retry)",
            replace_existing=True,
            misfire_grace_time=_MISFIRE_GRACE_SECONDS,
        )
        logger.info("Retry scheduled", extra={"job": job_name, "at": at.isoformat()})

    async def _save_next_run(self, job_name: str) -> None:
        async with self._sessions() as session:
            await SqlCollectionJobRepository(session).set_next_run(
                job_name, self.next_run(job_name)
            )
            await session.commit()
