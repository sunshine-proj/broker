"""Job configuration, the rate budget check and schedules, without a database."""

from datetime import UTC, datetime, time
from types import SimpleNamespace
from typing import Any

import pytest
from apscheduler.triggers.cron import CronTrigger
from pydantic import ValidationError

from app.collectors.scheduler import build_trigger
from app.config.settings import Settings
from app.models.enums import JobKind, ObservationType
from app.providers.coingecko import CoinGeckoProvider
from app.providers.ecb import EcbProvider
from app.providers.twelve_data import TwelveDataProvider
from app.schemas.provider_config import RatePeriod
from app.schemas.seed import SeedExchange, SeedFile, SeedJob
from app.services.budget import (
    BudgetExceededError,
    check_budget,
    is_session_open,
    plan_usage,
    runs_per_day,
)
from app.services.seed import load_seed_file

NYSE = {
    "code": "XNYS",
    "name": "New York Stock Exchange",
    "timezone": "America/New_York",
    "trading_open": "09:30",
    "trading_close": "16:00",
}


def job(**fields: Any) -> dict[str, Any]:
    return {"name": "test_job", "provider": "twelve_data", "kind": "quotes", **fields}


def seed(*, jobs: list[dict[str, Any]], limits: list[dict[str, Any]] | None = None) -> SeedFile:
    return SeedFile.model_validate(
        {
            "exchanges": [NYSE],
            "providers": [
                {
                    "name": "twelve_data",
                    "base_url": "https://api.twelvedata.com",
                    "configuration": {
                        "rate_limits": limits
                        or [{"limit": 8, "period": "minute"}, {"limit": 800, "period": "day"}]
                    },
                }
            ],
            "instruments": [
                {
                    "symbol": f"S{i}",
                    "name": f"Stock {i}",
                    "asset_type": "stock",
                    "exchange": "XNYS",
                    "currency": "USD",
                    "providers": {"twelve_data": f"S{i}"},
                }
                for i in range(10)
            ]
            + [
                {
                    "symbol": f"F{i}/USD",
                    "name": f"Pair {i}",
                    "asset_type": "forex",
                    "currency": "USD",
                    "providers": {"twelve_data": f"F{i}/USD"},
                }
                for i in range(5)
            ],
            "jobs": jobs,
        }
    )


# --- Job validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({}, "exactly one of interval_minutes and daily_at"),
        ({"interval_minutes": 15, "daily_at": "10:00"}, "exactly one"),
        ({"interval_minutes": 7}, "must divide 60"),
        ({"interval_minutes": 90}, "must divide 60"),
        ({"interval_minutes": 300}, "must divide 60"),
        ({"interval_minutes": 15, "lookback_days": 5}, "lookback_days does not apply"),
        ({"kind": "candles", "daily_at": "00:30", "lookback_days": 5}, "needs candle_interval"),
        ({"kind": "history", "daily_at": "00:30"}, "needs lookback_days"),
        (
            {
                "kind": "history",
                "daily_at": "00:30",
                "lookback_days": 5,
                "observation_types": ["price"],
            },
            "observation_types applies to quotes jobs only",
        ),
        (
            {
                "kind": "indicators",
                "daily_at": "00:30",
                "lookback_days": 5,
                "market_hours_only": True,
            },
            "do not apply to indicators",
        ),
        ({"interval_minutes": 15, "name": "Bad Name"}, "name"),
        ({"interval_minutes": 15, "frequency": "often"}, "frequency"),
    ],
)
def test_invalid_job_is_rejected(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SeedJob.model_validate(job(**fields))


@pytest.mark.parametrize("minutes", [1, 5, 15, 30, 60, 120, 360, 1440])
def test_clock_aligned_intervals_are_accepted(minutes: int) -> None:
    assert SeedJob.model_validate(job(interval_minutes=minutes)).interval_minutes == minutes


def test_job_settings_are_stored_as_json_values() -> None:
    parsed = SeedJob.model_validate(
        job(interval_minutes=5, asset_types=["stock"], observation_types=["price", "volume"])
    )

    assert parsed.settings() == {"asset_types": ["stock"], "observation_types": ["price", "volume"]}


def test_job_with_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown provider nope"):
        seed(jobs=[job(interval_minutes=15, provider="nope")])


def test_duplicate_job_names_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate job"):
        seed(jobs=[job(interval_minutes=15), job(daily_at="10:00")])


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"trading_open": "09:30"}, "set both"),
        ({"trading_open": "16:00", "trading_close": "09:30"}, "before"),
    ],
)
def test_invalid_session_is_rejected(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        SeedExchange.model_validate({**NYSE, "trading_open": None, "trading_close": None, **fields})


# --- Trading sessions ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "is_open"),
    [
        ("2026-11-16T14:30:00+00:00", True),  # Monday 09:30 New York (winter time)
        ("2026-11-16T14:29:00+00:00", False),
        ("2026-11-16T21:00:00+00:00", True),  # 16:00, the close, still counts
        ("2026-11-16T21:01:00+00:00", False),
        ("2026-07-15T13:30:00+00:00", True),  # 09:30 in summer time
        ("2026-07-15T14:30:00+00:00", True),
        ("2026-11-14T15:00:00+00:00", False),  # Saturday
    ],
)
def test_session_follows_local_time(at: str, is_open: bool) -> None:
    exchange = SeedExchange.model_validate(NYSE)

    assert is_session_open(exchange, datetime.fromisoformat(at)) is is_open


def test_exchange_without_a_session_is_always_open() -> None:
    exchange = SeedExchange(code="X", name="X", timezone="UTC")

    assert is_session_open(exchange, datetime(2026, 11, 14, 3, 0, tzinfo=UTC))


# --- Runs per day ------------------------------------------------------------------------------


def test_market_hours_job_counts_polls_within_the_session() -> None:
    config = seed(jobs=[job(interval_minutes=15, asset_types=["stock"], market_hours_only=True)])

    # 09:30 to 16:00 inclusive, every 15 minutes.
    assert runs_per_day(config, config.jobs[0]) == 27


def test_round_the_clock_and_daily_jobs() -> None:
    config = seed(jobs=[job(interval_minutes=15), job(name="daily", daily_at="00:30")])

    assert runs_per_day(config, config.jobs[0]) == 96
    assert runs_per_day(config, config.jobs[1]) == 1


def test_retry_is_counted_as_a_second_run() -> None:
    config = seed(jobs=[job(daily_at="00:30", retry_minutes=60)])

    assert runs_per_day(config, config.jobs[0]) == 2


# --- Budget ----------------------------------------------------------------------------------


def test_default_twelve_data_plan_fits() -> None:
    config = seed(
        jobs=[
            job(name="listed", interval_minutes=15, asset_types=["stock"], market_hours_only=True),
            job(name="forex", interval_minutes=15, asset_types=["forex"]),
            job(
                name="candles",
                kind="candles",
                candle_interval="1day",
                lookback_days=5,
                daily_at="00:30",
                retry_minutes=60,
            ),
        ]
    )

    usages = {u.period: u for u in check_budget(config)}

    assert usages[RatePeriod.DAY].planned == 10 * 27 + 5 * 96 + 15 * 2
    assert not usages[RatePeriod.MINUTE].exceeded


def test_over_budget_configuration_is_refused_naming_provider_and_jobs() -> None:
    config = seed(jobs=[job(name="every_minute", interval_minutes=1)])

    with pytest.raises(BudgetExceededError) as raised:
        check_budget(config)

    message = str(raised.value)
    assert "provider twelve_data would use 21600 credits per day" in message
    assert "limit of 800" in message
    assert "every_minute 15 x 1440/day" in message


def test_disabled_jobs_do_not_count() -> None:
    config = seed(jobs=[job(interval_minutes=1, enabled=False)])

    assert check_budget(config) == []


def test_monthly_limits_count_a_31_day_month() -> None:
    config = seed(
        jobs=[job(daily_at="00:30", asset_types=["forex"])],
        limits=[{"limit": 155, "period": "month"}],
    )

    (usage,) = plan_usage(config)
    assert usage.planned == 5 * 31
    assert not usage.exceeded


def test_shipped_seed_fits_every_budget(settings: Settings) -> None:
    usages = check_budget(load_seed_file(settings.seed_file))

    providers = {usage.provider for usage in usages}
    assert providers == {"twelve_data", "coingecko", "ecb"}
    assert all(not usage.exceeded for usage in usages)


def test_shipped_seed_jobs_cover_every_provider(settings: Settings) -> None:
    config = load_seed_file(settings.seed_file)

    assert {j.provider for j in config.jobs} == {p.name for p in config.providers}
    fred = [j for j in config.jobs if j.provider == "fred"]
    assert fred and not any(j.enabled for j in fred)


# --- Credits per run --------------------------------------------------------------------------


def test_twelve_data_charges_per_symbol() -> None:
    assert TwelveDataProvider.planned_credits(JobKind.QUOTES, ["A", "B", "C"]) == 3


def test_coingecko_batches_coins_and_counts_dominance_separately() -> None:
    coins = [f"coin{i}/usd" for i in range(10)] + ["bitcoin/eur"]
    market = set(ObservationType) - {ObservationType.DOMINANCE}

    assert CoinGeckoProvider.planned_credits(JobKind.QUOTES, coins, market) == 2
    assert CoinGeckoProvider.planned_credits(JobKind.QUOTES, coins, None) == 3
    assert (
        CoinGeckoProvider.planned_credits(JobKind.QUOTES, coins[:300], {ObservationType.PRICE}) == 2
    )
    assert CoinGeckoProvider.planned_credits(JobKind.HISTORY, coins, None) == 11


def test_ecb_quotes_cost_one_request_per_dataflow() -> None:
    series = [
        "EXR/D.USD.EUR.SP00.A",
        "EXR/D.GBP.EUR.SP00.A",
        "EST/B.EU000A2X2A25.WT",
        "FM/B.U2.EUR.4F.KR.DFR.LEV",
    ]

    assert EcbProvider.planned_credits(JobKind.QUOTES, series) == 3
    assert EcbProvider.planned_credits(JobKind.HISTORY, series) == 4


# --- Triggers ----------------------------------------------------------------------------------


def schedule(**fields: Any) -> Any:
    return SimpleNamespace(**{"name": "job", "interval_minutes": None, "daily_at": None, **fields})


def fires(trigger: CronTrigger, start: datetime, count: int) -> list[datetime]:
    times, previous = [], None
    now = start
    for _ in range(count):
        nxt = trigger.get_next_fire_time(previous, now)
        times.append(nxt)
        previous, now = nxt, nxt
    return times


def test_interval_below_an_hour_is_aligned_to_the_clock() -> None:
    trigger = build_trigger(schedule(interval_minutes=15))

    times = fires(trigger, datetime(2026, 11, 16, 9, 7, tzinfo=UTC), 3)

    assert [t.strftime("%H:%M") for t in times] == ["09:15", "09:30", "09:45"]


def test_hourly_intervals_fire_on_the_hour() -> None:
    trigger = build_trigger(schedule(interval_minutes=120))

    times = fires(trigger, datetime(2026, 11, 16, 9, 7, tzinfo=UTC), 2)

    assert [t.strftime("%H:%M") for t in times] == ["10:00", "12:00"]


def test_daily_schedule_fires_at_its_utc_time() -> None:
    trigger = build_trigger(schedule(daily_at=time(16, 0)))

    times = fires(trigger, datetime(2026, 11, 16, 17, 0, tzinfo=UTC), 1)

    assert times[0] == datetime(2026, 11, 17, 16, 0, tzinfo=UTC)
