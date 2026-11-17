"""Data-quality checks (PRD FR-012).

The checks read stored data only; they never call a provider and never change
a value. Each problem is recorded as a data-quality issue:

- stale: a series a job collects has nothing newer than the job allows;
- gap: two consecutive values of a series are further apart than the job allows;
- spike: a price-like value moved implausibly far from the one before (the
  value is marked suspect);
- discrepancy: two providers' latest values of the same instrument disagree.

Conflicts are recorded during ingestion, not here (see IngestionService).
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Self
from zoneinfo import ZoneInfo

from app.models import CollectionJob
from app.models.enums import CandleInterval, IssueKind, JobKind, ObservationType
from app.repositories.interfaces import MarketDataRepository
from app.repositories.quality import IssueIn, Series, SqlQualityRepository

logger = logging.getLogger(__name__)

CANDLE_MINUTES = {
    CandleInterval.MIN_1: 1,
    CandleInterval.MIN_5: 5,
    CandleInterval.MIN_15: 15,
    CandleInterval.MIN_30: 30,
    CandleInterval.HOUR_1: 60,
    CandleInterval.HOUR_4: 240,
    CandleInterval.DAY_1: 1440,
    CandleInterval.WEEK_1: 7 * 1440,
    CandleInterval.MONTH_1: 31 * 1440,
}

# A daily job's series is stale after this many days without a newer value.
_DAILY_STALE_DAYS = 4

# Most gaps reported per series and check, newest first. More than this is an
# outage, and the newest gaps are the ones backfill can still recover.
MAX_GAPS_PER_SERIES = 20


@dataclass(frozen=True)
class QualitySettings:
    """A quality job's settings, with the PRD's defaults."""

    lookback_days: int = 30
    spike_percent: Decimal = Decimal(20)
    spike_points: Decimal = Decimal(1)
    discrepancy_percent: Decimal = Decimal(2)
    discrepancy_points: Decimal = Decimal("0.1")

    @classmethod
    def from_job(cls, settings: dict[str, Any]) -> Self:
        values: dict[str, Any] = {}
        if "lookback_days" in settings:
            values["lookback_days"] = int(settings["lookback_days"])
        for name in ("spike_percent", "spike_points", "discrepancy_percent", "discrepancy_points"):
            if name in settings:
                values[name] = Decimal(str(settings[name]))
        return cls(**values)


@dataclass(frozen=True)
class QualityReport:
    stale: int = 0
    gaps: int = 0
    spikes: int = 0
    discrepancies: int = 0
    # Issues that are new or were reopened by this check.
    opened: int = 0
    # Issues closed because their problem has gone.
    closed: int = 0


def business_seconds(start: datetime, end: datetime) -> float:
    """Seconds between start and end, counting Monday to Friday (UTC) only."""
    if end <= start:
        return 0.0
    total = 0.0
    at = start.astimezone(UTC)
    end = end.astimezone(UTC)
    while at < end:
        midnight = datetime(at.year, at.month, at.day, tzinfo=UTC) + timedelta(days=1)
        segment_end = min(end, midnight)
        if at.weekday() < 5:
            total += (segment_end - at).total_seconds()
        at = segment_end
    return total


def stale_after(job: CollectionJob) -> timedelta:
    explicit = job.settings.get("stale_after_minutes")
    if explicit:
        return timedelta(minutes=int(explicit))
    if job.interval_minutes:
        return timedelta(minutes=3 * job.interval_minutes)
    return timedelta(days=_DAILY_STALE_DAYS)


def gap_after(job: CollectionJob) -> timedelta | None:
    explicit = job.settings.get("gap_after_minutes")
    if explicit:
        return timedelta(minutes=int(explicit))
    if job.kind is JobKind.CANDLES:
        return timedelta(minutes=CANDLE_MINUTES[CandleInterval(job.settings["candle_interval"])])
    return None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 72 else f"{hours // 24} days"


class QualityService:
    def __init__(
        self,
        repository: SqlQualityRepository,
        market_data: MarketDataRepository,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._repository = repository
        self._market_data = market_data
        self._clock = clock

    async def check(
        self,
        jobs: Sequence[CollectionJob],
        providers: dict[uuid.UUID, str],
        settings: QualitySettings,
        since: datetime,
    ) -> QualityReport:
        """Run every check. `since`: values stored after it are checked for spikes."""
        now = self._clock()
        stale_found: set[str] = set()
        stale = gaps = opened = 0

        for job in jobs:
            if not job.enabled or job.kind not in (
                JobKind.QUOTES,
                JobKind.CANDLES,
                JobKind.HISTORY,
            ):
                continue
            assert job.provider_id is not None
            provider = providers[job.provider_id]
            series = await self._repository.series(job.provider_id, job.settings.get("asset_types"))
            keep, count, new = await self._stale(job, provider, series, now)
            stale_found |= keep
            stale += count
            opened += new
            count, new = await self._gaps(job, provider, series, now, settings)
            gaps += count
            opened += new

        closed = await self._repository.close_others(IssueKind.STALE, stale_found, now)
        spikes = await self._spikes(settings, since, now)
        discrepancies, new, closed_discrepancies = await self._discrepancies(settings, now)
        report = QualityReport(
            stale=stale,
            gaps=gaps,
            spikes=spikes,
            discrepancies=discrepancies,
            opened=opened + new,
            closed=closed + closed_discrepancies,
        )
        logger.info("Data quality checked", extra=report.__dict__)
        return report

    # --- Stale ------------------------------------------------------------------------

    async def _stale(
        self, job: CollectionJob, provider: str, series: Sequence[Series], now: datetime
    ) -> tuple[set[str], int, int]:
        """Fingerprints to keep open (stale, or not checkable now), stale series, new issues."""
        assert job.provider_id is not None
        limit = stale_after(job)
        business = bool(job.settings.get("business_days_only"))
        by_id = {s.instrument_id: s for s in series}
        ids = list(by_id)
        if job.kind is JobKind.CANDLES:
            interval = CandleInterval(job.settings["candle_interval"])
            latest = await self._repository.latest_candles(job.provider_id, ids, interval)
            what = f"{interval.value} candles"
        else:
            types = job.settings.get("observation_types")
            latest = await self._repository.latest_observations(
                job.provider_id, ids, {ObservationType(t) for t in types} if types else None
            )
            interval = None
            what = ""

        found: set[str] = set()
        stale = opened = 0
        for item in latest:
            entry = by_id[item.instrument_id]
            if entry.published_on_change:
                # Recorded only when it changes: an old value is the current one.
                continue
            label = what or item.observation_type.value  # type: ignore[union-attr]
            fingerprint = f"stale:{job.provider_id}:{item.instrument_id}:{label}"
            if job.market_hours_only and not _open_long_enough(entry, now, limit):
                # Not checkable while the market is closed: an open issue stays open.
                found.add(fingerprint)
                continue
            age = (
                business_seconds(item.observed_at, now)
                if business
                else (now - item.observed_at).total_seconds()
            )
            if age <= limit.total_seconds():
                continue
            found.add(fingerprint)
            stale += 1
            opened += await self._repository.report(
                IssueIn(
                    kind=IssueKind.STALE,
                    fingerprint=fingerprint,
                    message=(
                        f"{entry.symbol} {label} from {provider}: nothing newer than "
                        f"{item.observed_at:%Y-%m-%d %H:%M} UTC ({_duration(age)}; "
                        f"expected within {_duration(limit.total_seconds())})"
                    ),
                    instrument_id=item.instrument_id,
                    provider_id=job.provider_id,
                    observation_type=item.observation_type,
                    interval=interval,
                    range_start=item.observed_at,
                    details={"job": job.name, "age_seconds": int(age)},
                ),
                now,
            )
        return found, stale, opened

    # --- Gaps -------------------------------------------------------------------------

    async def _gaps(
        self,
        job: CollectionJob,
        provider: str,
        series: Sequence[Series],
        now: datetime,
        settings: QualitySettings,
    ) -> tuple[int, int]:
        assert job.provider_id is not None
        limit = gap_after(job)
        if limit is None:
            return 0, 0
        checked = {s.instrument_id: s for s in series if not s.published_on_change}
        # Only what the job has collected: history imported from before it
        # existed is often coarser than the job's own interval.
        since = now - timedelta(days=settings.lookback_days)
        if job.created_at is not None:
            since = max(since, job.created_at)
        seconds = int(limit.total_seconds())
        interval: CandleInterval | None = None
        if job.kind is JobKind.CANDLES:
            interval = CandleInterval(job.settings["candle_interval"])
            gaps = await self._repository.candle_gaps(
                job.provider_id, list(checked), interval, since, seconds
            )
        else:
            types = job.settings.get("observation_types")
            gaps = await self._repository.observation_gaps(
                job.provider_id,
                list(checked),
                {ObservationType(t) for t in types} if types else None,
                since,
                seconds,
            )

        business = bool(job.settings.get("business_days_only"))
        found = opened = 0
        per_series: dict[tuple[Any, ...], int] = {}
        for gap in sorted(gaps, key=lambda g: g.start, reverse=True):
            if business and business_seconds(gap.start, gap.end) <= seconds:
                continue
            key = (gap.instrument_id, gap.observation_type)
            per_series[key] = per_series.get(key, 0) + 1
            if per_series[key] > MAX_GAPS_PER_SERIES:
                continue
            found += 1
            label = interval.value if interval else gap.observation_type.value  # type: ignore[union-attr]
            entry = checked[gap.instrument_id]
            opened += await self._repository.report(
                IssueIn(
                    kind=IssueKind.GAP,
                    fingerprint=(
                        f"gap:{job.provider_id}:{gap.instrument_id}:{label}:{gap.start.isoformat()}"
                    ),
                    message=(
                        f"{entry.symbol} {label} from {provider}: no values between "
                        f"{gap.start:%Y-%m-%d %H:%M} and {gap.end:%Y-%m-%d %H:%M} UTC"
                    ),
                    instrument_id=gap.instrument_id,
                    provider_id=job.provider_id,
                    observation_type=gap.observation_type,
                    interval=interval,
                    range_start=gap.start,
                    range_end=gap.end,
                    details={"job": job.name},
                ),
                now,
            )
        return found, opened

    # --- Spikes -----------------------------------------------------------------------

    async def _spikes(self, settings: QualitySettings, since: datetime, now: datetime) -> int:
        spikes = await self._repository.spikes(since, settings.spike_percent, settings.spike_points)
        for spike in spikes:
            if spike.unit == "percent":
                change = f"{spike.value - spike.previous_value:+} points"
            else:
                change = f"{(spike.value / spike.previous_value - 1) * 100:+.1f} %"
            await self._repository.record_event(
                IssueIn(
                    kind=IssueKind.SPIKE,
                    fingerprint=(
                        f"spike:{spike.provider_id}:{spike.instrument_id}:"
                        f"{spike.observation_type.value}:{spike.observed_at.isoformat()}"
                    ),
                    message=(
                        f"{spike.observation_type.value} moved {change} from the value before; "
                        "marked suspect"
                    ),
                    instrument_id=spike.instrument_id,
                    provider_id=spike.provider_id,
                    observation_type=spike.observation_type,
                    range_start=spike.previous_at,
                    range_end=spike.observed_at,
                    details={"value": spike.value, "previous_value": spike.previous_value},
                ),
                now,
            )
        await self._market_data.mark_suspect(
            [
                {
                    "instrument_id": s.instrument_id,
                    "provider_id": s.provider_id,
                    "observation_type": s.observation_type,
                    "observed_at": s.observed_at,
                }
                for s in spikes
            ]
        )
        return len(spikes)

    # --- Discrepancies ----------------------------------------------------------------

    async def _discrepancies(
        self, settings: QualitySettings, now: datetime
    ) -> tuple[int, int, int]:
        found: set[str] = set()
        opened = 0
        for pair in await self._repository.discrepancies():
            difference = pair.value - pair.other_value
            if pair.unit == "percent":
                if abs(difference) <= settings.discrepancy_points:
                    continue
                size = f"{difference:+} points"
            else:
                if pair.other_value == 0:
                    continue
                percent = abs(difference / pair.other_value) * 100
                if percent <= settings.discrepancy_percent:
                    continue
                size = f"{difference / pair.other_value * 100:+.2f} %"
            fingerprint = (
                f"discrepancy:{pair.instrument_id}:{pair.observation_type.value}:"
                f"{pair.provider_id}:{pair.other_provider_id}"
            )
            found.add(fingerprint)
            opened += await self._repository.report(
                IssueIn(
                    kind=IssueKind.DISCREPANCY,
                    fingerprint=fingerprint,
                    message=(
                        f"{pair.symbol} {pair.observation_type.value}: {pair.provider} "
                        f"{pair.value} against {pair.other_provider} {pair.other_value} ({size})"
                    ),
                    instrument_id=pair.instrument_id,
                    provider_id=pair.provider_id,
                    other_provider_id=pair.other_provider_id,
                    observation_type=pair.observation_type,
                    range_start=min(pair.observed_at, pair.other_observed_at),
                    range_end=max(pair.observed_at, pair.other_observed_at),
                    details={
                        "value": pair.value,
                        "other_value": pair.other_value,
                        "observed_at": pair.observed_at.isoformat(),
                        "other_observed_at": pair.other_observed_at.isoformat(),
                    },
                ),
                now,
            )
        closed = await self._repository.close_others(IssueKind.DISCREPANCY, found, now)
        return len(found), opened, closed


def _open_long_enough(entry: Series, now: datetime, limit: timedelta) -> bool:
    """Whether the instrument's session has been open at least `limit` (always, without one)."""
    if entry.exchange_timezone is None or entry.trading_open is None:
        return True
    local = now.astimezone(ZoneInfo(entry.exchange_timezone))
    if local.weekday() >= 5:
        return False
    opened = local.replace(
        hour=entry.trading_open.hour, minute=entry.trading_open.minute, second=0, microsecond=0
    )
    closes = local.replace(
        hour=entry.trading_close.hour, minute=entry.trading_close.minute, second=0, microsecond=0
    )
    return opened + limit <= local <= closes
