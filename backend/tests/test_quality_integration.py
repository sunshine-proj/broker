"""Data quality against the test database (PRD FR-012), plus restart and outage scenarios.

The checks run against data written straight into the database. Backfill,
restart and outage scenarios run jobs through the JobRunner with an in-memory
provider, `fake`, whose market the tests control.
"""

import uuid
from collections.abc import AsyncIterator, Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.collectors.runner import JobRunner
from app.collectors.scheduler import CollectorScheduler
from app.config.settings import Settings
from app.database import create_session_factory
from app.main import create_app
from app.models import CollectionJob, CollectorExecution, DataQualityIssue, ProviderHealth
from app.models.enums import (
    CandleInterval,
    CollectorStatus,
    ExecutionTrigger,
    Freshness,
    IssueKind,
    IssueResolution,
    IssueStatus,
    ObservationType,
    ProviderHealthStatus,
    QuoteKind,
)
from app.providers.base import Capability, Provider
from app.providers.errors import ProviderUnavailableError
from app.providers.factory import ADAPTERS, ProviderFactory
from app.repositories.collection_jobs import SqlCollectionJobRepository
from app.repositories.economic_data import SqlEconomicDataRepository
from app.repositories.instruments import SqlInstrumentRepository
from app.repositories.market_data import SqlMarketDataRepository
from app.repositories.providers import SqlProviderRepository
from app.repositories.quality import SqlQualityRepository
from app.schemas.market_data import MarketObservationIn, OHLCVIn
from app.schemas.normalized import Candle, Quote
from app.schemas.seed import SeedFile
from app.services.ingestion import IngestionService
from app.services.quality import QualityService, QualitySettings
from app.services.seed import apply_seed
from app.services.storage_policies import StoragePolicies, apply_storage_policies

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

# Wednesday 2026-11-18, 11:00 in New York: the session has been open 90 minutes.
NOW = datetime(2026, 11, 18, 16, 0, tzinfo=UTC)
SATURDAY = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)


# --- An in-memory provider -------------------------------------------------------------


@dataclass
class Market:
    """What the fake providers serve: prices by symbol, and whether they are down."""

    prices: dict[str, list[tuple[datetime, Decimal]]] = field(default_factory=dict)
    candles: dict[str, list[tuple[datetime, Decimal]]] = field(default_factory=dict)
    down: bool = False
    calls: list[str] = field(default_factory=list)


MARKET = Market()


class FakeProvider(Provider):
    name = "fake"
    capabilities = frozenset({Capability.QUOTES, Capability.QUOTE_HISTORY, Capability.CANDLES})

    def _check(self, call: str) -> None:
        MARKET.calls.append(call)
        if MARKET.down:
            raise ProviderUnavailableError(self.name, "server error (HTTP 503)")

    def _quote(self, symbol: str, at: datetime, value: Decimal) -> Quote:
        return Quote(
            provider_symbol=symbol,
            observation_type=ObservationType.PRICE,
            observed_at=at,
            value=value,
            currency="USD",
            unit="currency",
            freshness=Freshness.REAL_TIME,
            quote_kind=QuoteKind.TRADED,
        )

    async def fetch_quotes(
        self, symbols: Sequence[str], observation_types: Collection[ObservationType] | None = None
    ) -> list[Quote]:
        self._check("quotes")
        return [self._quote(s, *MARKET.prices[s][-1]) for s in symbols if MARKET.prices.get(s)]

    async def fetch_quote_history(self, symbol: str, start: datetime, end: datetime) -> list[Quote]:
        self._check(f"history {symbol}")
        return [
            self._quote(symbol, t, v) for t, v in MARKET.prices.get(symbol, []) if start <= t < end
        ]

    async def fetch_candles(
        self, symbol: str, interval: CandleInterval, start: datetime, end: datetime
    ) -> list[Candle]:
        self._check(f"candles {symbol}")
        return [
            Candle(
                provider_symbol=symbol,
                interval=interval,
                timestamp=t,
                open=v,
                high=v,
                low=v,
                close=v,
                currency="USD",
            )
            for t, v in MARKET.candles.get(symbol, [])
            if start <= t < end
        ]


class OtherProvider(FakeProvider):
    name = "other"


class NoHistoryProvider(FakeProvider):
    name = "nohistory"
    capabilities = frozenset({Capability.QUOTES})


SEED: dict[str, Any] = {
    "exchanges": [
        {
            "code": "XNYS",
            "name": "NYSE",
            "timezone": "America/New_York",
            "trading_open": "09:30",
            "trading_close": "16:00",
        }
    ],
    "providers": [
        {"name": name, "base_url": f"https://{name}.example.test"}
        for name in ("fake", "other", "nohistory")
    ],
    "instruments": [
        {
            "symbol": "BTC/USD",
            "name": "Bitcoin",
            "asset_type": "crypto",
            "currency": "USD",
            "providers": {"fake": "BTC", "nohistory": "BTC"},
        },
        {
            "symbol": "AAA",
            "name": "AAA Corp",
            "asset_type": "stock",
            "exchange": "XNYS",
            "currency": "USD",
            "providers": {"fake": "AAA"},
        },
        {
            "symbol": "EUR/USD",
            "name": "Euro",
            "asset_type": "forex",
            "currency": "USD",
            "providers": {"fake": "EURUSD", "other": "EURUSD"},
        },
        {
            "symbol": "POLICY",
            "name": "A policy rate",
            "asset_type": "interest_rate",
            "metadata": {"published_on_change": True},
            "providers": {"fake": "POLICY"},
        },
    ],
    "jobs": [
        {
            "name": "crypto_quotes",
            "provider": "fake",
            "kind": "quotes",
            "asset_types": ["crypto", "interest_rate"],
            "interval_minutes": 15,
            "gap_after_minutes": 60,
        },
        {
            "name": "stock_quotes",
            "provider": "fake",
            "kind": "quotes",
            "asset_types": ["stock"],
            "interval_minutes": 15,
            "market_hours_only": True,
        },
        {
            "name": "daily_candles",
            "provider": "fake",
            "kind": "candles",
            "asset_types": ["stock"],
            "candle_interval": "1day",
            "lookback_days": 5,
            "daily_at": "00:30",
            "business_days_only": True,
        },
        {
            "name": "nohistory_quotes",
            "provider": "nohistory",
            "kind": "quotes",
            "interval_minutes": 15,
            "gap_after_minutes": 60,
        },
        {"name": "quality", "kind": "quality", "interval_minutes": 15, "lookback_days": 30},
        {
            "name": "fake_backfill",
            "provider": "fake",
            "kind": "backfill",
            "daily_at": "03:00",
            "max_requests": 5,
        },
        {
            "name": "nohistory_backfill",
            "provider": "nohistory",
            "kind": "backfill",
            "daily_at": "03:00",
            "max_requests": 5,
        },
    ],
}


@dataclass
class World:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    providers: ProviderFactory
    clock: list[datetime]
    ids: dict[str, uuid.UUID]

    def now(self) -> datetime:
        return self.clock[0]

    def runner(self) -> JobRunner:
        return JobRunner(
            engine=self.engine,
            session_factory=self.sessions,
            provider_factory=self.providers,
            clock=self.now,
        )

    async def run(self, job: str, trigger: ExecutionTrigger = ExecutionTrigger.SCHEDULED) -> Any:
        return await self.runner().run(job, trigger)

    async def check(self, since: datetime | None = None) -> Any:
        async with self.sessions() as session:
            jobs = await SqlCollectionJobRepository(session).list(enabled=True)
            names = {self.ids[n]: n for n in ("fake", "other", "nohistory")}
            report = await QualityService(
                SqlQualityRepository(session), SqlMarketDataRepository(session), clock=self.now
            ).check(jobs, names, QualitySettings(), since or self.now() - timedelta(hours=1))
            await session.commit()
        return report

    async def prices(
        self, symbol: str, provider: str, *points: tuple[datetime, str], unit: str = "currency"
    ) -> None:
        async with self.sessions() as session:
            await SqlMarketDataRepository(session).add_observations(
                [
                    MarketObservationIn(
                        instrument_id=self.ids[symbol],
                        provider_id=self.ids[provider],
                        observation_type=ObservationType.RATE
                        if unit == "percent"
                        else ObservationType.PRICE,
                        observed_at=at,
                        value=Decimal(value),
                        currency=None if unit == "percent" else "USD",
                        unit=unit,
                        freshness=Freshness.REAL_TIME,
                        quote_kind=QuoteKind.TRADED,
                    )
                    for at, value in points
                ]
            )
            await session.commit()

    async def candles(self, symbol: str, *days: datetime) -> None:
        async with self.sessions() as session:
            await SqlMarketDataRepository(session).add_candles(
                [
                    OHLCVIn(
                        instrument_id=self.ids[symbol],
                        provider_id=self.ids["fake"],
                        interval=CandleInterval.DAY_1,
                        timestamp=day,
                        open=Decimal(1),
                        high=Decimal(1),
                        low=Decimal(1),
                        close=Decimal(1),
                        currency="USD",
                    )
                    for day in days
                ]
            )
            await session.commit()

    async def issues(self, kind: IssueKind | None = None) -> list[DataQualityIssue]:
        async with self.sessions() as session:
            statement = sa.select(DataQualityIssue).order_by(DataQualityIssue.range_start)
            if kind is not None:
                statement = statement.where(DataQualityIssue.kind == kind)
            return list((await session.execute(statement)).scalars())

    async def scalar(self, query: str, **params: Any) -> Any:
        async with self.sessions() as session:
            return await session.scalar(sa.text(query), params)


@pytest.fixture
async def world(
    migrated_database: Settings, empty_database: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[World]:
    for adapter in (FakeProvider, OtherProvider, NoHistoryProvider):
        monkeypatch.setitem(ADAPTERS, adapter.name, adapter)
    MARKET.prices.clear()
    MARKET.candles.clear()
    MARKET.calls.clear()
    MARKET.down = False
    sessions = create_session_factory(empty_database)
    async with sessions() as session:
        await apply_seed(
            SeedFile.model_validate(SEED),
            providers=SqlProviderRepository(session),
            instruments=SqlInstrumentRepository(session),
            economic_data=SqlEconomicDataRepository(session),
            jobs=SqlCollectionJobRepository(session),
        )
        # Jobs created long before the test data, so gaps are checked from the lookback on.
        await session.execute(sa.text("UPDATE collection_job SET created_at = '2026-01-01'"))
        await session.commit()
        ids = {
            name: id_
            for name, id_ in (await session.execute(sa.text("SELECT name, id FROM provider"))).all()
        } | {
            symbol: id_
            for symbol, id_ in (
                await session.execute(sa.text("SELECT symbol, id FROM instrument"))
            ).all()
        }
    providers = ProviderFactory(migrated_database, http=httpx.AsyncClient())
    try:
        yield World(empty_database, sessions, providers, [NOW], ids)
    finally:
        await providers.aclose()


def every(start: datetime, end: datetime, minutes: int) -> list[datetime]:
    points, at = [], start
    while at < end:
        points.append(at)
        at += timedelta(minutes=minutes)
    return points


# --- Stale -------------------------------------------------------------------------------


async def test_stale_series_are_reported_cleared_and_reopened(world: World) -> None:
    await world.prices("BTC/USD", "fake", (NOW - timedelta(hours=2), "100"))

    report = await world.check()

    (issue,) = await world.issues(IssueKind.STALE)
    assert (report.stale, report.opened) == (1, 1)
    assert issue.status is IssueStatus.OPEN
    assert issue.instrument_id == world.ids["BTC/USD"]
    assert "expected within 45 min" in issue.message
    assert issue.range_start == NOW - timedelta(hours=2)

    # Checked again: the same issue, not a new one.
    assert (await world.check()).opened == 0

    await world.prices("BTC/USD", "fake", (NOW - timedelta(minutes=1), "101"))
    report = await world.check()
    (issue,) = await world.issues(IssueKind.STALE)
    assert (issue.status, issue.resolution, report.closed) == (
        IssueStatus.CLOSED,
        IssueResolution.CLEARED,
        1,
    )

    world.clock[0] = NOW + timedelta(hours=1)
    assert (await world.check()).opened == 1
    (issue,) = await world.issues(IssueKind.STALE)
    assert (issue.status, issue.resolution, issue.detected_at) == (
        IssueStatus.OPEN,
        None,
        NOW + timedelta(hours=1),
    )


@pytest.mark.parametrize(
    ("now", "stale"),
    [
        (NOW, True),  # open for 90 minutes, more than 3 intervals
        (datetime(2026, 11, 18, 14, 40, tzinfo=UTC), False),  # open for 10 minutes
        (datetime(2026, 11, 18, 22, 0, tzinfo=UTC), False),  # closed
        (SATURDAY, False),
    ],
)
async def test_market_hours_series_are_checked_only_while_the_session_runs(
    world: World, now: datetime, stale: bool
) -> None:
    await world.prices("AAA", "fake", (datetime(2026, 11, 17, 21, 0, tzinfo=UTC), "50"))
    world.clock[0] = now

    await world.check()

    assert bool(await world.issues(IssueKind.STALE)) is stale


async def test_a_stale_market_hours_issue_stays_open_after_the_close(world: World) -> None:
    await world.prices("AAA", "fake", (datetime(2026, 11, 17, 21, 0, tzinfo=UTC), "50"))
    await world.check()

    world.clock[0] = datetime(2026, 11, 18, 22, 0, tzinfo=UTC)
    await world.check()

    (issue,) = await world.issues(IssueKind.STALE)
    assert issue.status is IssueStatus.OPEN


# --- Gaps --------------------------------------------------------------------------------


async def test_gaps_are_reported_once_with_their_range(world: World) -> None:
    start = NOW - timedelta(hours=10)
    hole = (NOW - timedelta(hours=6), NOW - timedelta(hours=3))
    points = [t for t in every(start, NOW, 15) if not hole[0] < t < hole[1]]
    await world.prices("BTC/USD", "fake", *[(t, "100") for t in points])

    report = await world.check()
    await world.check()

    (gap,) = await world.issues(IssueKind.GAP)
    assert report.gaps == 1
    assert (gap.range_start, gap.range_end) == (hole[0], hole[1])
    assert (gap.observation_type, gap.status) == (ObservationType.PRICE, IssueStatus.OPEN)


async def test_values_from_before_the_job_existed_are_not_checked_for_gaps(world: World) -> None:
    start = NOW - timedelta(hours=10)
    hole = (NOW - timedelta(hours=6), NOW - timedelta(hours=3))
    points = [t for t in every(start, NOW, 15) if not hole[0] < t < hole[1]]
    await world.prices("BTC/USD", "fake", *[(t, "100") for t in points])
    async with world.sessions() as session:
        await session.execute(
            sa.text("UPDATE collection_job SET created_at = :at WHERE name = 'crypto_quotes'"),
            {"at": NOW - timedelta(hours=2)},
        )
        await session.commit()

    await world.check()

    assert await world.issues(IssueKind.GAP) == []


async def test_rates_published_on_change_are_not_checked_for_gaps_or_staleness(
    world: World,
) -> None:
    await world.prices(
        "POLICY",
        "fake",
        (NOW - timedelta(days=20), "4.0"),
        (NOW - timedelta(days=5), "3.75"),
        unit="percent",
    )

    await world.check()

    assert await world.issues() == []


async def test_business_day_candles_skip_weekends_but_not_missing_weekdays(world: World) -> None:
    friday, monday, wednesday = (datetime(2026, 11, d, tzinfo=UTC) for d in (13, 16, 18))
    await world.candles("AAA", friday, monday, wednesday)

    await world.check()

    (gap,) = await world.issues(IssueKind.GAP)
    assert (gap.range_start, gap.range_end, gap.interval) == (
        monday,
        wednesday,
        CandleInterval.DAY_1,
    )


# --- Spikes and discrepancies ------------------------------------------------------------


async def test_spikes_are_recorded_and_marked_suspect(world: World) -> None:
    before = NOW - timedelta(hours=2)
    await world.prices("BTC/USD", "fake", (before, "100"))
    await world.prices(
        "BTC/USD", "fake", (NOW - timedelta(minutes=10), "150"), (NOW - timedelta(minutes=5), "151")
    )
    # A rate in percent moves in points: 0.5 is normal, 1.5 is not.
    await world.prices("POLICY", "fake", (before, "3.0"), unit="percent")
    await world.prices(
        "POLICY",
        "fake",
        (NOW - timedelta(minutes=9), "3.5"),
        (NOW - timedelta(minutes=4), "5.0"),
        unit="percent",
    )

    # Stored just now (ingested_at is the database's clock, not the test's).
    report = await world.check(since=datetime.now(UTC) - timedelta(minutes=5))

    spikes = await world.issues(IssueKind.SPIKE)
    assert report.spikes == 2
    assert [(s.instrument_id, s.range_end) for s in spikes] == [
        (world.ids["BTC/USD"], NOW - timedelta(minutes=10)),
        (world.ids["POLICY"], NOW - timedelta(minutes=4)),
    ]
    assert all(
        (s.status, s.resolution) == (IssueStatus.CLOSED, IssueResolution.RECORDED) for s in spikes
    )
    suspect = await world.scalar(
        "SELECT array_agg(value ORDER BY value) FROM market_observation"
        " WHERE quality_status = 'suspect'"
    )
    assert suspect == [Decimal("5.0"), Decimal("150")]


async def test_provider_discrepancies_open_and_clear(world: World) -> None:
    earlier, latest = NOW - timedelta(hours=3), NOW - timedelta(minutes=5)
    await world.prices("EUR/USD", "fake", (earlier, "1.10"), (latest, "1.10"))
    # The other provider's latest value is older: it is compared with the
    # first provider's value at its time, not with the latest one.
    await world.prices("EUR/USD", "other", (earlier, "1.20"))

    assert (await world.check()).discrepancies == 1
    (issue,) = await world.issues(IssueKind.DISCREPANCY)
    assert {issue.provider_id, issue.other_provider_id} == {world.ids["fake"], world.ids["other"]}
    assert "1.10" in issue.message and "1.20" in issue.message
    assert (issue.range_start, issue.range_end) == (earlier, earlier)

    await world.prices("EUR/USD", "other", (latest, "1.105"))
    assert (await world.check()).discrepancies == 0
    (issue,) = await world.issues(IssueKind.DISCREPANCY)
    assert issue.resolution is IssueResolution.CLEARED


async def test_values_far_apart_in_time_are_not_compared(world: World) -> None:
    await world.prices("EUR/USD", "fake", (NOW - timedelta(minutes=5), "1.10"))
    await world.prices("EUR/USD", "other", (NOW - timedelta(days=3), "1.50"))

    assert (await world.check()).discrepancies == 0


# --- Conflicts (duplicate observations) ----------------------------------------------------


async def test_conflicting_duplicates_are_recorded_and_the_stored_value_marked_suspect(
    world: World,
) -> None:
    at = NOW - timedelta(minutes=5)
    quote = Quote(
        provider_symbol="BTC",
        observation_type=ObservationType.PRICE,
        observed_at=at,
        value=Decimal("100.0"),
        currency="USD",
        unit="currency",
        freshness=Freshness.REAL_TIME,
        quote_kind=QuoteKind.TRADED,
    )
    async with world.sessions() as session:
        ingestion = IngestionService(
            instruments=SqlInstrumentRepository(session),
            market_data=SqlMarketDataRepository(session),
            economic_data=SqlEconomicDataRepository(session),
            quality=SqlQualityRepository(session),
            clock=world.now,
        )
        await ingestion.ingest_quotes(world.ids["fake"], [quote])
        # The same value again (written differently) is a harmless duplicate.
        same = await ingestion.ingest_quotes(
            world.ids["fake"], [quote.model_copy(update={"value": Decimal("100")})]
        )
        different = await ingestion.ingest_quotes(
            world.ids["fake"], [quote.model_copy(update={"value": Decimal("99")})]
        )
        await session.commit()

    assert (same.unchanged, same.conflicting) == (1, 0)
    assert different.conflicting == 1
    (conflict,) = await world.issues(IssueKind.CONFLICT)
    assert (conflict.status, conflict.resolution) == (IssueStatus.CLOSED, IssueResolution.RECORDED)
    assert conflict.details == {"stored_value": "100.0", "incoming_value": "99"}
    assert (
        await world.scalar("SELECT value || ' ' || quality_status FROM market_observation")
        == "100.0 suspect"
    )


# --- Backfill ------------------------------------------------------------------------------


async def make_gap(
    world: World, symbol: str = "BTC/USD", provider: str = "fake"
) -> tuple[datetime, datetime]:
    """Values every 15 minutes up to now, except for a three-hour hole; then check."""
    hole = (NOW - timedelta(hours=6), NOW - timedelta(hours=3))
    points = [hole[0], *every(hole[1], NOW, 15)]
    await world.prices(symbol, provider, *[(t, "100") for t in points])
    await world.check()
    return hole


async def test_backfill_fills_a_gap_from_the_providers_history(world: World) -> None:
    hole = await make_gap(world)
    inside = [hole[0] + timedelta(hours=h) for h in (1, 2)]
    MARKET.prices["BTC"] = [
        (hole[0], Decimal(100)),
        *[(t, Decimal(101)) for t in inside],
        (hole[1], Decimal(100)),
    ]

    result = await world.run("fake_backfill")

    assert (result.status, result.records_written) == (CollectorStatus.SUCCESS, 2)
    (gap,) = await world.issues(IssueKind.GAP)
    assert (gap.status, gap.resolution, gap.details["values_found"]) == (
        IssueStatus.CLOSED,
        IssueResolution.FILLED,
        2,
    )
    # The values at either end were not asked for again.
    assert MARKET.calls == ["history BTC"]
    # A filled gap is not reported again.
    await world.check()
    assert len(await world.issues(IssueKind.GAP)) == 1


async def test_a_gap_the_provider_has_nothing_for_is_closed_as_no_data(world: World) -> None:
    await make_gap(world)

    await world.run("fake_backfill")

    (gap,) = await world.issues(IssueKind.GAP)
    assert gap.resolution is IssueResolution.NO_DATA


async def test_a_gap_without_provider_history_is_unrecoverable(world: World) -> None:
    await make_gap(world, provider="nohistory")

    result = await world.run("nohistory_backfill")

    (gap,) = await world.issues(IssueKind.GAP)
    assert gap.resolution is IssueResolution.UNRECOVERABLE
    assert gap.details["reason"] == "the provider offers no quote history"
    assert result.status is CollectorStatus.SUCCESS
    assert MARKET.calls == []


async def test_a_failing_backfill_gives_up_after_three_attempts(world: World) -> None:
    await make_gap(world)
    MARKET.down = True

    statuses = [(await world.run("fake_backfill")).status for _ in range(3)]

    assert statuses == [CollectorStatus.FAILED] * 3
    (gap,) = await world.issues(IssueKind.GAP)
    assert (gap.resolution, gap.details["attempts"]) == (IssueResolution.UNRECOVERABLE, 3)


# --- API ---------------------------------------------------------------------------------


async def test_issues_are_served_by_the_api(world: World, migrated_database: Settings) -> None:
    await make_gap(world)
    await world.prices("EUR/USD", "fake", (NOW - timedelta(minutes=5), "1.10"))
    await world.prices("EUR/USD", "other", (NOW - timedelta(minutes=5), "1.30"))
    await world.check()

    with TestClient(create_app(migrated_database)) as client:
        window = {"start": "2026-11-01T00:00:00Z", "end": "2026-12-01T00:00:00Z"}
        everything = client.get("/api/v1/quality/issues", params=window).json()
        gaps = client.get("/api/v1/quality/issues", params=window | {"kind": "gap"}).json()
        other = client.get("/api/v1/quality/issues", params=window | {"provider": "other"}).json()
        unknown = client.get("/api/v1/quality/issues", params={"provider": "nope"})

    assert {i["kind"] for i in everything["items"]} == {"gap", "discrepancy"}
    (gap,) = gaps["items"]
    assert (gap["symbol"], gap["provider"], gap["status"], gap["observation_type"]) == (
        "BTC/USD",
        "fake",
        "open",
        "price",
    )
    assert gap["range_start"] == "2026-11-18T10:00:00Z"
    (discrepancy,) = other["items"]
    assert {discrepancy["provider"], discrepancy["other_provider"]} == {"fake", "other"}
    assert unknown.status_code == 422


# --- The quality job --------------------------------------------------------------------


async def test_the_quality_job_runs_without_a_provider(world: World) -> None:
    await world.prices("BTC/USD", "fake", (NOW - timedelta(hours=2), "100"))

    result = await world.run("quality")

    assert (result.status, result.records_written) == (CollectorStatus.SUCCESS, 1)
    async with world.sessions() as session:
        execution = await session.get(CollectorExecution, result.execution_id)
        assert execution is not None and execution.provider_id is None


# --- Storage policies ---------------------------------------------------------------------


async def test_storage_policies_are_applied_and_replaced(world: World) -> None:
    query = """SELECT proc_name || ':' || hypertable_name FROM timescaledb_information.jobs
                WHERE hypertable_name IN ('market_observation', 'ohlcv') ORDER BY 1"""
    try:
        async with world.engine.begin() as connection:
            await apply_storage_policies(
                connection, StoragePolicies(compress_after_days=30, observation_retention_days=400)
            )
        async with world.engine.connect() as connection:
            policies = (await connection.execute(sa.text(query))).scalars().all()
        assert policies == [
            "policy_compression:market_observation",
            "policy_compression:ohlcv",
            "policy_retention:market_observation",
        ]

        async with world.engine.begin() as connection:
            await apply_storage_policies(connection, StoragePolicies(compress_after_days=None))
        async with world.engine.connect() as connection:
            assert (await connection.execute(sa.text(query))).scalars().all() == []
    finally:
        async with world.engine.begin() as connection:
            await apply_storage_policies(connection, StoragePolicies(compress_after_days=None))


async def test_compressed_history_still_takes_new_rows_conflicts_and_quality_status(
    world: World,
) -> None:
    old = datetime(2025, 1, 6, 12, 0, tzinfo=UTC)
    await world.prices("BTC/USD", "fake", (old, "100"))
    async with world.engine.begin() as connection:
        compressed = (
            await connection.execute(
                sa.text(
                    "SELECT compress_chunk(c) FROM show_chunks('market_observation', "
                    "older_than => TIMESTAMPTZ '2025-02-01') c"
                )
            )
        ).all()
    assert compressed

    async with world.sessions() as session:
        market = SqlMarketDataRepository(session)
        late = await market.add_observations(
            [
                MarketObservationIn(
                    instrument_id=world.ids["BTC/USD"],
                    provider_id=world.ids["fake"],
                    observation_type=ObservationType.PRICE,
                    observed_at=at,
                    value=Decimal(value),
                    currency="USD",
                    unit="currency",
                    freshness=Freshness.REAL_TIME,
                    quote_kind=QuoteKind.TRADED,
                )
                for at, value in ((old, "99"), (old + timedelta(hours=1), "101"))
            ]
        )
        marked = await market.mark_suspect([late.conflicts[0].key])
        await session.commit()

    assert (late.inserted, late.conflicting, marked) == (1, 1, 1)
    assert (
        await world.scalar(
            "SELECT string_agg(value || ' ' || quality_status, ', ' ORDER BY observed_at)"
            " FROM market_observation"
        )
        == "100 suspect, 101 ok"
    )


# --- Restart and outage -------------------------------------------------------------------


async def test_a_run_cut_off_by_a_restart_is_closed_and_the_job_runs_again(world: World) -> None:
    MARKET.prices["BTC"] = [(NOW - timedelta(minutes=1), Decimal(100))]
    await world.run("crypto_quotes")

    # The process dies during a run: the execution stays "running" and the
    # job's lock goes with the database connection.
    handle = await world.runner().prepare("crypto_quotes", ExecutionTrigger.SCHEDULED)
    assert handle is not None
    await handle._connection.invalidate()

    scheduler = CollectorScheduler(
        runner=world.runner(), session_factory=world.sessions, clock=world.now
    )
    try:
        scheduled = await scheduler.start()
    finally:
        await scheduler.shutdown()

    assert "crypto_quotes" in scheduled
    async with world.sessions() as session:
        interrupted = await session.get(CollectorExecution, handle.execution_id)
        assert interrupted is not None
        assert (interrupted.status, interrupted.error) == (
            CollectorStatus.FAILED,
            "interrupted: the process stopped before the run finished",
        )
    # The data from before the restart is still there, and the job runs again.
    assert await world.scalar("SELECT count(*) FROM market_observation") == 1
    MARKET.prices["BTC"].append((NOW, Decimal(101)))
    assert (await world.run("crypto_quotes")).status is CollectorStatus.SUCCESS
    assert await world.scalar("SELECT count(*) FROM market_observation") == 2


async def test_a_provider_outage_is_reported_then_recovered_and_backfilled(world: World) -> None:
    start = NOW - timedelta(hours=4)
    MARKET.prices["BTC"] = [(start, Decimal(100))]
    world.clock[0] = start
    assert (await world.run("crypto_quotes")).status is CollectorStatus.SUCCESS

    # Down for two hours: every run fails and the provider is marked down.
    MARKET.down = True
    for minutes in range(15, 120, 15):
        world.clock[0] = start + timedelta(minutes=minutes)
        MARKET.prices["BTC"].append((world.clock[0], Decimal(100)))
        assert (await world.run("crypto_quotes")).status is CollectorStatus.FAILED
    async with world.sessions() as session:
        health = await session.get(ProviderHealth, world.ids["fake"])
        assert health is not None and health.status is ProviderHealthStatus.DOWN
    assert (await world.run("quality")).status is CollectorStatus.SUCCESS
    (stale,) = await world.issues(IssueKind.STALE)
    assert stale.status is IssueStatus.OPEN

    # Back up: collection resumes, the stale issue clears and the hole is a gap.
    MARKET.down = False
    world.clock[0] = start + timedelta(minutes=120)
    MARKET.prices["BTC"].append((world.clock[0], Decimal(100)))
    assert (await world.run("crypto_quotes")).status is CollectorStatus.SUCCESS
    await world.run("quality")
    (stale,) = await world.issues(IssueKind.STALE)
    assert stale.resolution is IssueResolution.CLEARED
    (gap,) = await world.issues(IssueKind.GAP)
    assert (gap.range_start, gap.range_end) == (start, start + timedelta(minutes=120))

    # Backfill re-fetches the missed values from the provider's history.
    assert (await world.run("fake_backfill")).records_written == 7
    (gap,) = await world.issues(IssueKind.GAP)
    assert gap.resolution is IssueResolution.FILLED
    async with world.sessions() as session:
        health = await session.get(ProviderHealth, world.ids["fake"])
        assert health is not None and health.status is ProviderHealthStatus.HEALTHY
    assert (
        await world.scalar(
            "SELECT count(*) FROM market_observation WHERE instrument_id = :i",
            i=world.ids["BTC/USD"],
        )
        == 9
    )


async def test_job_settings_reach_the_database(world: World) -> None:
    async with world.sessions() as session:
        job = await session.scalar(
            sa.select(CollectionJob).where(CollectionJob.name == "daily_candles")
        )
        quality = await session.scalar(
            sa.select(CollectionJob).where(CollectionJob.name == "quality")
        )
    assert job is not None and job.settings["business_days_only"] is True
    assert quality is not None and quality.provider_id is None
