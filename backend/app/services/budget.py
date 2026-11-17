"""Provider rate budget check (PRD FR-011).

Before the seed file is applied, the planned usage of every enabled job is
added up per provider and compared with each of the provider's configured
limits. A configuration that would exceed a limit is refused, naming the
provider, the limit and the jobs that use it.

The estimate is deliberately pessimistic:
- market-hours jobs are counted as if every day were a trading day;
- a job with a retry is counted as if every run were retried once;
- a month is counted as 31 days.
Limits shorter than a day are compared with the average use over the day.
Bursts within a minute are paced at run time by the rate limiter.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import app.providers  # noqa: F401  (registers the adapters)
from app.models.enums import JobKind
from app.providers.base import Provider
from app.providers.factory import ADAPTERS
from app.schemas.provider_config import ProviderSettings, RatePeriod
from app.schemas.seed import SeedExchange, SeedFile, SeedJob

_DAYS_PER_PERIOD = {
    RatePeriod.SECOND: 1 / 86400,
    RatePeriod.MINUTE: 1 / 1440,
    RatePeriod.HOUR: 1 / 24,
    RatePeriod.DAY: 1,
    RatePeriod.MONTH: 31,
}

# Days on which to count market-hours runs: one in winter and one in summer
# time, so a session that moves against UTC is counted at its larger extent.
_SAMPLE_DAYS = (date(2026, 1, 14), date(2026, 7, 15))


class BudgetExceededError(Exception):
    """The configured jobs would use more than a provider allows."""


@dataclass(frozen=True)
class JobUsage:
    job: str
    credits_per_run: int
    runs_per_day: float

    @property
    def credits_per_day(self) -> float:
        return self.credits_per_run * self.runs_per_day


@dataclass(frozen=True)
class LimitUsage:
    provider: str
    period: RatePeriod
    limit: int
    planned: float
    jobs: tuple[JobUsage, ...]

    @property
    def exceeded(self) -> bool:
        return self.planned > self.limit


def job_symbols(seed: SeedFile, job: SeedJob) -> list[str]:
    """The provider symbols or series codes a job works on."""
    if job.kind is JobKind.INDICATORS:
        return [
            indicator.provider_series_code
            for indicator in seed.indicators
            if indicator.provider == job.provider and indicator.enabled
        ]
    return [
        instrument.providers[job.provider]
        for instrument in seed.instruments
        if job.provider in instrument.providers
        and instrument.enabled
        and (job.asset_types is None or instrument.asset_type in job.asset_types)
    ]


def fire_times(job: SeedJob, day: date) -> list[datetime]:
    """When the job's schedule fires on a given UTC day."""
    midnight = datetime(day.year, day.month, day.day, tzinfo=UTC)
    if job.daily_at is not None:
        return [midnight.replace(hour=job.daily_at.hour, minute=job.daily_at.minute)]
    assert job.interval_minutes is not None
    return [midnight + timedelta(minutes=minute) for minute in range(0, 1440, job.interval_minutes)]


def is_session_open(exchange: SeedExchange, at: datetime) -> bool:
    if exchange.trading_open is None or exchange.trading_close is None:
        return True
    local = at.astimezone(ZoneInfo(exchange.timezone))
    if local.weekday() >= 5:
        return False
    return exchange.trading_open <= local.time().replace(tzinfo=None) <= exchange.trading_close


def runs_per_day(seed: SeedFile, job: SeedJob) -> float:
    """Runs on a busy day, counting a retry for every run if the job has one."""
    if not job.market_hours_only:
        runs = float(len(fire_times(job, _SAMPLE_DAYS[0])))
    else:
        exchanges = _job_exchanges(seed, job)
        runs = float(
            max(
                sum(
                    1
                    for at in fire_times(job, _weekday(day))
                    if any(is_session_open(exchange, at) for exchange in exchanges)
                )
                for day in _SAMPLE_DAYS
            )
        )
    return runs * 2 if job.retry_minutes is not None else runs


def _job_exchanges(seed: SeedFile, job: SeedJob) -> list[SeedExchange]:
    by_code = {exchange.code: exchange for exchange in seed.exchanges}
    always_open = SeedExchange(code="-", name="-", timezone="UTC")
    exchanges = []
    for instrument in seed.instruments:
        if job.provider not in instrument.providers or not instrument.enabled:
            continue
        if job.asset_types is not None and instrument.asset_type not in job.asset_types:
            continue
        exchanges.append(by_code[instrument.exchange] if instrument.exchange else always_open)
    return exchanges or [always_open]


def _weekday(day: date) -> date:
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def plan_usage(seed: SeedFile) -> list[LimitUsage]:
    """Planned use of every configured limit of every provider that has jobs."""
    usages = []
    for provider in seed.providers:
        jobs = [job for job in seed.jobs if job.provider == provider.name and job.enabled]
        if not jobs or not provider.enabled:
            continue
        adapter = ADAPTERS.get(provider.name)
        if adapter is None:
            raise BudgetExceededError(f"provider {provider.name} has jobs but no adapter")
        job_usages = tuple(
            JobUsage(
                job=job.name,
                credits_per_run=_credits_per_run(adapter, seed, job),
                runs_per_day=runs_per_day(seed, job),
            )
            for job in jobs
        )
        per_day = sum(usage.credits_per_day for usage in job_usages)
        usages += [
            LimitUsage(
                provider=provider.name,
                period=limit.period,
                limit=limit.limit,
                planned=per_day * _DAYS_PER_PERIOD[limit.period],
                jobs=job_usages,
            )
            for limit in _limits(provider.configuration)
        ]
    return usages


def _credits_per_run(adapter: type[Provider], seed: SeedFile, job: SeedJob) -> int:
    if job.kind is JobKind.BACKFILL:
        # Each request re-fetches one series; count it as a candle request,
        # the dearest kind for every adapter.
        assert job.max_requests is not None
        return job.max_requests * max(1, adapter.planned_credits(JobKind.CANDLES, ["-"]))
    return adapter.planned_credits(job.kind, job_symbols(seed, job), job.observation_types)


def _limits(settings: ProviderSettings) -> Sequence:
    return settings.rate_limits


def check_budget(seed: SeedFile) -> list[LimitUsage]:
    """Raise BudgetExceededError if any provider's planned use exceeds a limit."""
    usages = plan_usage(seed)
    exceeded = [usage for usage in usages if usage.exceeded]
    if exceeded:
        raise BudgetExceededError("; ".join(_describe(usage) for usage in exceeded))
    return usages


def _describe(usage: LimitUsage) -> str:
    jobs = ", ".join(
        f"{job.job} {job.credits_per_run} x {job.runs_per_day:g}/day" for job in usage.jobs
    )
    return (
        f"provider {usage.provider} would use {usage.planned:.0f} credits per "
        f"{usage.period.value}, over its limit of {usage.limit} ({jobs})"
    )


def is_open_now(
    exchange_timezone: str, trading_open: time | None, trading_close: time | None, at: datetime
) -> bool:
    """Whether an exchange's regular session is open at `at` (holidays are not modelled)."""
    return is_session_open(
        SeedExchange(
            code="-",
            name="-",
            timezone=exchange_timezone,
            trading_open=trading_open,
            trading_close=trading_close,
        ),
        at,
    )
