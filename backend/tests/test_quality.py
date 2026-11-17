"""Data quality without a database: thresholds, settings and their validation (PRD FR-012)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.config.settings import Settings
from app.models import CollectionJob
from app.models.enums import JobKind
from app.services.budget import check_budget, plan_usage
from app.services.quality import QualitySettings, business_seconds, gap_after, stale_after
from app.services.seed import load_seed_file
from tests.test_jobs_config import job, seed

DAY = 86400.0


def at(day: int, hour: int = 0) -> datetime:
    # November 2026: the 13th is a Friday.
    return datetime(2026, 11, day, hour, tzinfo=UTC)


@pytest.mark.parametrize(
    ("start", "end", "seconds"),
    [
        (at(16), at(17), DAY),  # Monday
        (at(13), at(16), DAY),  # Friday to Monday: the weekend is not counted
        (at(13, 22), at(15, 21), 2 * 3600),  # forex close to reopen: Friday evening only
        (at(14), at(16), 0),  # Saturday and Sunday
        (at(13), at(20), 5 * DAY),  # a whole week
        (at(16, 12), at(16, 6), 0),  # end before start
    ],
)
def test_business_seconds_count_weekdays_only(
    start: datetime, end: datetime, seconds: float
) -> None:
    assert business_seconds(start, end) == seconds


def collection_job(**fields: Any) -> CollectionJob:
    values: dict[str, Any] = {"name": "j", "kind": JobKind.QUOTES, "settings": {}}
    return CollectionJob(**(values | fields))


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"interval_minutes": 15}, timedelta(minutes=45)),
        ({"interval_minutes": 1}, timedelta(minutes=3)),
        ({"daily_at": "16:00"}, timedelta(days=4)),
        ({"interval_minutes": 30, "settings": {"stale_after_minutes": 240}}, timedelta(hours=4)),
    ],
)
def test_stale_thresholds_default_to_three_intervals_or_four_days(
    fields: dict[str, Any], expected: timedelta
) -> None:
    assert stale_after(collection_job(**fields)) == expected


def test_gap_thresholds_default_to_none_for_quotes_and_the_interval_for_candles() -> None:
    assert gap_after(collection_job(interval_minutes=5)) is None
    assert gap_after(
        collection_job(interval_minutes=5, settings={"gap_after_minutes": 30})
    ) == timedelta(minutes=30)
    candles = collection_job(kind=JobKind.CANDLES, settings={"candle_interval": "1day"})
    assert gap_after(candles) == timedelta(days=1)


def test_quality_settings_have_the_prd_defaults_and_read_job_settings() -> None:
    assert QualitySettings() == QualitySettings.from_job({})
    settings = QualitySettings.from_job(
        {"lookback_days": 7, "spike_percent": "35", "discrepancy_points": "0.25"}
    )
    assert (settings.lookback_days, settings.spike_percent, settings.discrepancy_points) == (
        7,
        Decimal(35),
        Decimal("0.25"),
    )
    assert (settings.spike_points, settings.discrepancy_percent) == (Decimal(1), Decimal(2))


# --- Seed file ----------------------------------------------------------------------------


def test_quality_and_backfill_jobs_are_valid() -> None:
    config = seed(
        jobs=[
            {
                "name": "quality",
                "kind": "quality",
                "interval_minutes": 15,
                "lookback_days": 30,
                "spike_percent": "25",
            },
            job(name="backfill", kind="backfill", daily_at="03:00", max_requests=5),
            job(
                interval_minutes=15,
                stale_after_minutes=60,
                gap_after_minutes=60,
                business_days_only=True,
            ),
        ]
    )

    quality, backfill, quotes = config.jobs
    assert quality.provider is None
    assert quality.settings() == {"lookback_days": 30, "spike_percent": "25"}
    assert backfill.settings() == {"max_requests": 5}
    assert quotes.settings() == {
        "stale_after_minutes": 60,
        "gap_after_minutes": 60,
        "business_days_only": True,
    }


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        (
            {"kind": "quality", "interval_minutes": 15, "lookback_days": 1},
            "a quality job has no provider",
        ),
        ({"kind": "quotes", "provider": None, "interval_minutes": 15}, "the job needs a provider"),
        ({"kind": "backfill", "daily_at": "03:00"}, "a backfill job needs max_requests"),
        ({"interval_minutes": 15, "max_requests": 3}, "max_requests applies to backfill jobs only"),
        (
            {"kind": "backfill", "daily_at": "03:00", "max_requests": 3, "lookback_days": 3},
            "lookback_days does not apply to backfill jobs",
        ),
        (
            {"interval_minutes": 15, "spike_percent": 5},
            "spike_percent applies to quality jobs only",
        ),
        (
            {"kind": "indicators", "daily_at": "03:00", "lookback_days": 3, "gap_after_minutes": 5},
            "gap_after_minutes applies to quotes, candles and history jobs only",
        ),
        ({"interval_minutes": 15, "stale_after_minutes": 0}, "greater than 0"),
    ],
)
def test_misplaced_quality_settings_are_refused(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        seed(jobs=[job(**fields)])


def test_backfill_requests_count_against_the_budget() -> None:
    config = seed(
        jobs=[job(name="backfill", kind="backfill", daily_at="03:00", max_requests=50)],
        limits=[{"limit": 40, "period": "day"}],
    )

    (usage,) = plan_usage(config)
    assert usage.planned == 50
    with pytest.raises(Exception, match="over its limit of 40"):
        check_budget(config)


def test_shipped_seed_checks_quality_and_backfills_every_collecting_provider(
    settings: Settings,
) -> None:
    config = load_seed_file(settings.seed_file)

    (quality,) = [j for j in config.jobs if j.kind is JobKind.QUALITY]
    assert quality.interval_minutes == 15
    collecting = {
        j.provider for j in config.jobs if j.enabled and j.kind is not JobKind.QUALITY
    } - {None}
    backfilled = {j.provider for j in config.jobs if j.kind is JobKind.BACKFILL}
    # FRED's jobs are disabled, so it has nothing to backfill.
    assert backfilled == collecting
    # Rates that only change at central bank decisions are not checked for gaps.
    key_rates = [i for i in config.instruments if i.symbol in ("ECB-DFR", "ECB-MRO")]
    assert all(i.metadata.get("published_on_change") for i in key_rates)


# --- Storage settings ---------------------------------------------------------------------


@pytest.fixture
def storage_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("COMPRESS_AFTER_DAYS", "OBSERVATION_RETENTION_DAYS", "CANDLE_RETENTION_DAYS"):
        monkeypatch.delenv(name, raising=False)


def make_settings(**fields: Any) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        postgres_db="unit",
        postgres_user="unit",
        postgres_password="unit",
        **fields,
    )


@pytest.mark.usefixtures("storage_env")
def test_storage_settings_default_to_compression_and_no_retention() -> None:
    settings = make_settings()

    assert (
        settings.compress_after_days,
        settings.observation_retention_days,
        settings.candle_retention_days,
    ) == (30, None, None)


@pytest.mark.usefixtures("storage_env")
def test_empty_storage_settings_mean_never_and_zero_is_refused() -> None:
    settings = make_settings(
        compress_after_days="", observation_retention_days=" ", candle_retention_days="365"
    )
    assert (settings.compress_after_days, settings.observation_retention_days) == (None, None)
    assert settings.candle_retention_days == 365

    with pytest.raises(ValidationError):
        make_settings(observation_retention_days=0)
