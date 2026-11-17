"""Metrics read from the database at each scrape (PRD FR-013).

They describe the whole platform, including runs started from the command
line, so they come from the stored state rather than from this process. Each
scrape builds them afresh, so a provider or job that disappears also
disappears from the metrics.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

import sqlalchemy as sa
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import IssueKind

HEALTH_VALUES = {"healthy": 0, "degraded": 1, "down": 2}


@dataclass(frozen=True)
class ProviderState:
    provider: str
    health: str
    consecutive_failures: int


@dataclass(frozen=True)
class JobState:
    job: str
    kind: str
    market_hours: bool
    failed: bool
    last_success: datetime | None
    expected_interval_seconds: int


@dataclass(frozen=True)
class DataAge:
    provider: str
    asset_type: str
    observation_type: str
    age_seconds: float


@dataclass(frozen=True)
class BackupState:
    # The newest backup run, whatever its outcome.
    last_failed: bool
    interval_seconds: int
    # The newest successful one, if any.
    last_success: datetime | None
    last_success_size_bytes: int | None


@dataclass(frozen=True)
class DatabaseSnapshot:
    providers: list[ProviderState] = field(default_factory=list)
    jobs: list[JobState] = field(default_factory=list)
    data_age: list[DataAge] = field(default_factory=list)
    open_issues: dict[str, int] = field(default_factory=dict)
    stale_series: dict[str, int] = field(default_factory=dict)
    # None until the backup service has run once.
    backup: BackupState | None = None


async def read_snapshot(session: AsyncSession) -> DatabaseSnapshot:
    providers = [
        ProviderState(*row)
        for row in await session.execute(
            # Providers in use: enabled, with an enabled job. One whose jobs are
            # all off (FRED by default) would otherwise alert on old failures.
            sa.text(
                """
                SELECT s.provider, s.health, s.consecutive_failures
                  FROM v_provider_status s JOIN provider p ON p.name = s.provider
                 WHERE s.enabled
                   AND EXISTS (SELECT 1 FROM collection_job j
                                WHERE j.provider_id = p.id AND j.enabled)
                 ORDER BY s.provider
                """
            )
        )
    ]
    jobs = [
        JobState(
            job=row.name,
            kind=row.kind,
            market_hours=row.market_hours_only,
            failed=row.last_status == "failed",
            last_success=row.last_success,
            expected_interval_seconds=(row.interval_minutes or 1440) * 60,
        )
        for row in await session.execute(
            sa.text(
                """
                SELECT j.name, j.kind, j.market_hours_only, j.interval_minutes, j.last_status,
                       (SELECT e.finished_at FROM collector_execution e
                         WHERE e.job_name = j.name AND e.status = 'success'
                         ORDER BY e.started_at DESC LIMIT 1) AS last_success
                  FROM collection_job j
                 WHERE j.enabled
                 ORDER BY j.name
                """
            )
        )
    ]
    data_age = [
        DataAge(*row)
        for row in await session.execute(
            sa.text(
                "SELECT provider, asset_type, observation_type, age_seconds FROM v_data_freshness"
            )
        )
    ]
    open_issues = {kind.value: 0 for kind in IssueKind}
    for kind, count in await session.execute(
        sa.text("SELECT kind, count(*) FROM data_quality_issue WHERE status = 'open' GROUP BY kind")
    ):
        open_issues[kind] = count
    stale_series = {state.provider: 0 for state in providers}
    for provider, count in await session.execute(
        sa.text(
            """
            SELECT p.name, count(*)
              FROM data_quality_issue q JOIN provider p ON p.id = q.provider_id
             WHERE q.kind = 'stale' AND q.status = 'open' GROUP BY p.name
            """
        )
    ):
        stale_series[provider] = count
    backup = None
    latest = (
        await session.execute(
            sa.text(
                """
                SELECT b.status, b.interval_seconds, s.finished_at, s.size_bytes
                  FROM (SELECT status, interval_seconds FROM database_backup
                         ORDER BY started_at DESC LIMIT 1) b
                  LEFT JOIN LATERAL (
                       SELECT finished_at, size_bytes FROM database_backup
                        WHERE status = 'success' ORDER BY started_at DESC LIMIT 1) s ON true
                """
            )
        )
    ).first()
    if latest is not None:
        backup = BackupState(latest[0] == "failed", latest[1], latest[2], latest[3])
    return DatabaseSnapshot(providers, jobs, data_age, open_issues, stale_series, backup)


class _SnapshotCollector(Collector):
    def __init__(self, snapshot: DatabaseSnapshot | None) -> None:
        self._snapshot = snapshot

    def collect(self) -> Iterator[GaugeMetricFamily]:
        snapshot = self._snapshot
        yield GaugeMetricFamily(
            "marketdata_database_up",
            "1 if the backend could read the database at this scrape, else 0.",
            value=0 if snapshot is None else 1,
        )
        if snapshot is None:
            return

        health = GaugeMetricFamily(
            "marketdata_provider_health_status",
            "Provider health: 0 healthy, 1 degraded, 2 down. Absent until first used.",
            labels=["provider"],
        )
        failures = GaugeMetricFamily(
            "marketdata_provider_consecutive_failures",
            "Failed provider calls in a row.",
            labels=["provider"],
        )
        for state in snapshot.providers:
            if state.health in HEALTH_VALUES:
                health.add_metric([state.provider], HEALTH_VALUES[state.health])
            failures.add_metric([state.provider], state.consecutive_failures)
        yield health
        yield failures

        failed = GaugeMetricFamily(
            "marketdata_job_failed",
            "1 if the enabled job's last finished run failed, else 0.",
            labels=["job_name", "kind", "market_hours"],
        )
        last_success = GaugeMetricFamily(
            "marketdata_job_last_success_timestamp_seconds",
            "When the job's last successful run finished (Unix time).",
            labels=["job_name", "kind", "market_hours"],
        )
        interval = GaugeMetricFamily(
            "marketdata_job_expected_interval_seconds",
            "How often the job is scheduled: its interval, or a day for daily jobs.",
            labels=["job_name", "kind", "market_hours"],
        )
        for job in snapshot.jobs:
            labels = [job.job, job.kind, "true" if job.market_hours else "false"]
            failed.add_metric(labels, 1 if job.failed else 0)
            interval.add_metric(labels, job.expected_interval_seconds)
            if job.last_success is not None:
                last_success.add_metric(labels, job.last_success.timestamp())
        yield failed
        yield last_success
        yield interval

        age = GaugeMetricFamily(
            "marketdata_data_age_seconds",
            "Age of the newest value per provider, asset type and observation type.",
            labels=["provider", "asset_type", "observation_type"],
        )
        for item in snapshot.data_age:
            age.add_metric(
                [item.provider, item.asset_type, item.observation_type], float(item.age_seconds)
            )
        yield age

        issues = GaugeMetricFamily(
            "marketdata_quality_open_issues",
            "Open data-quality issues, by kind.",
            labels=["kind"],
        )
        for kind, count in snapshot.open_issues.items():
            issues.add_metric([kind], count)
        yield issues

        stale = GaugeMetricFamily(
            "marketdata_stale_series",
            "Series with an open stale issue, per provider.",
            labels=["provider"],
        )
        for provider, count in snapshot.stale_series.items():
            stale.add_metric([provider], count)
        yield stale

        backup = snapshot.backup
        if backup is None:
            return
        yield GaugeMetricFamily(
            "marketdata_backup_last_failed",
            "1 if the newest database backup run failed, else 0.",
            value=1 if backup.last_failed else 0,
        )
        yield GaugeMetricFamily(
            "marketdata_backup_expected_interval_seconds",
            "How often the backup service makes a backup.",
            value=backup.interval_seconds,
        )
        if backup.last_success is not None:
            yield GaugeMetricFamily(
                "marketdata_backup_last_success_timestamp_seconds",
                "When the newest successful database backup finished (Unix time).",
                value=backup.last_success.timestamp(),
            )
            yield GaugeMetricFamily(
                "marketdata_backup_last_size_bytes",
                "Size of the newest successful database backup.",
                value=backup.last_success_size_bytes or 0,
            )


def render(snapshot: DatabaseSnapshot | None) -> bytes:
    """The database metrics in the text exposition format; `None` means the database is down."""
    registry = CollectorRegistry(auto_describe=False)
    registry.register(_SnapshotCollector(snapshot))
    return generate_latest(registry)
