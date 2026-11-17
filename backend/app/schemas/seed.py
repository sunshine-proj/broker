"""Schema of the seed file (see backend/seed.toml)."""

from datetime import time
from decimal import Decimal
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.enums import (
    AssetType,
    CandleInterval,
    IndicatorFrequency,
    JobKind,
    ObservationType,
)
from app.schemas.provider_config import ProviderSettings


class _SeedModel(BaseModel):
    # Unknown keys are rejected so that a typo cannot be silently ignored.
    model_config = ConfigDict(extra="forbid")


class SeedExchange(_SeedModel):
    code: str
    name: str
    country: str | None = Field(default=None, min_length=2, max_length=2)
    timezone: str
    # Regular session in local time, Monday to Friday, for example "09:30".
    trading_open: time | None = None
    trading_close: time | None = None

    @model_validator(mode="after")
    def _check_session(self) -> Self:
        if (self.trading_open is None) != (self.trading_close is None):
            raise ValueError(f"exchange {self.code}: set both trading_open and trading_close")
        if (
            self.trading_open is not None
            and self.trading_close is not None
            and self.trading_open >= self.trading_close
        ):
            raise ValueError(f"exchange {self.code}: trading_open must be before trading_close")
        return self


class SeedProvider(_SeedModel):
    name: str
    base_url: str = Field(pattern=r"^https://")
    enabled: bool = True
    # Timeouts, retries, rate limits and authentication. Never a credential.
    configuration: ProviderSettings = Field(default_factory=ProviderSettings)


class SeedInstrument(_SeedModel):
    symbol: str
    name: str
    asset_type: AssetType
    exchange: str | None = None
    currency: str | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Provider name -> the symbol that provider uses for this instrument.
    providers: dict[str, str] = Field(default_factory=dict)


class SeedIndicator(_SeedModel):
    code: str
    name: str
    country: str | None = Field(default=None, min_length=2, max_length=2)
    unit: str
    frequency: IndicatorFrequency
    provider: str
    provider_series_code: str
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


_COLLECTING = frozenset({JobKind.QUOTES, JobKind.CANDLES, JobKind.HISTORY})
_QUALITY_FIELDS = ("spike_percent", "spike_points", "discrepancy_percent", "discrepancy_points")
_SERIES_FIELDS = ("stale_after_minutes", "gap_after_minutes")


class SeedJob(_SeedModel):
    name: str = Field(pattern=r"^[a-z0-9_]+$", max_length=128)
    # Every job but a quality job belongs to one provider.
    provider: str | None = None
    kind: JobKind
    # Exactly one schedule: every N minutes, aligned to the clock, or daily at a UTC time.
    interval_minutes: int | None = Field(default=None, gt=0)
    daily_at: time | None = None
    # Poll only instruments whose exchange is in its regular session.
    market_hours_only: bool = False
    # Minutes after a failed run to try once more.
    retry_minutes: int | None = Field(default=None, gt=0)
    # Limit the job to instruments of these asset types (quotes, candles, history).
    asset_types: list[AssetType] | None = None
    # Limit a quotes job to these observation types.
    observation_types: list[ObservationType] | None = None
    candle_interval: CandleInterval | None = None
    # How far back candles, history and indicators jobs fetch, and how far back
    # a quality job looks for gaps.
    lookback_days: int | None = Field(default=None, gt=0)
    enabled: bool = True

    # Data quality of what a quotes, candles or history job collects (PRD FR-012).
    # A series is stale with no value newer than this; default three intervals,
    # or four days for a daily job.
    stale_after_minutes: int | None = Field(default=None, gt=0)
    # Consecutive values further apart than this are a gap. No default for
    # quotes and history; the candle interval for candles.
    gap_after_minutes: int | None = Field(default=None, gt=0)
    # Count only Monday to Friday (UTC) time, for markets closed at weekends.
    business_days_only: bool = False

    # Backfill: most requests per run.
    max_requests: int | None = Field(default=None, gt=0)

    # Quality: thresholds. Percent applies to values in money, points to values
    # in percent (rates, yields). Defaults: 20 %, 1 point, 2 %, 0.1 points.
    spike_percent: Decimal | None = Field(default=None, gt=0)
    spike_points: Decimal | None = Field(default=None, gt=0)
    discrepancy_percent: Decimal | None = Field(default=None, gt=0)
    discrepancy_points: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _check(self) -> Self:
        problems = []
        if (self.interval_minutes is None) == (self.daily_at is None):
            problems.append("set exactly one of interval_minutes and daily_at")
        if self.interval_minutes is not None and not _aligned_interval(self.interval_minutes):
            problems.append(
                "interval_minutes must divide 60, or be a whole number of hours dividing 24"
            )
        if self.kind is JobKind.CANDLES and self.candle_interval is None:
            problems.append("a candles job needs candle_interval")
        if self.kind is not JobKind.CANDLES and self.candle_interval is not None:
            problems.append("candle_interval applies to candles jobs only")
        if self.kind is not JobKind.QUOTES and self.observation_types is not None:
            problems.append("observation_types applies to quotes jobs only")
        if self.kind is JobKind.QUOTES and self.lookback_days is not None:
            problems.append("lookback_days does not apply to quotes jobs")
        if self.kind is JobKind.BACKFILL and self.lookback_days is not None:
            problems.append("lookback_days does not apply to backfill jobs")
        if self.kind not in (JobKind.QUOTES, JobKind.BACKFILL) and self.lookback_days is None:
            problems.append(f"a {self.kind.value} job needs lookback_days")
        if self.kind in (JobKind.INDICATORS, JobKind.QUALITY, JobKind.BACKFILL) and (
            self.asset_types or self.market_hours_only
        ):
            problems.append(
                f"asset_types and market_hours_only do not apply to {self.kind.value} jobs"
            )
        if (self.kind is JobKind.QUALITY) != (self.provider is None):
            problems.append(
                "a quality job has no provider"
                if self.kind is JobKind.QUALITY
                else "the job needs a provider"
            )
        if (self.kind is JobKind.BACKFILL) != (self.max_requests is not None):
            problems.append(
                "a backfill job needs max_requests"
                if self.kind is JobKind.BACKFILL
                else "max_requests applies to backfill jobs only"
            )
        if self.kind is not JobKind.QUALITY:
            problems += [
                f"{field} applies to quality jobs only"
                for field in _QUALITY_FIELDS
                if getattr(self, field) is not None
            ]
        if self.kind not in _COLLECTING:
            problems += [
                f"{field} applies to quotes, candles and history jobs only"
                for field in (*_SERIES_FIELDS, "business_days_only")
                if getattr(self, field)
            ]
        if problems:
            raise ValueError(f"job {self.name}: " + "; ".join(problems))
        return self

    def settings(self) -> dict[str, Any]:
        """The filters, windows and data-quality settings stored with the job, as JSON values."""
        settings = self.model_dump(
            mode="json",
            include={
                "asset_types",
                "observation_types",
                "candle_interval",
                "lookback_days",
                "max_requests",
                *_SERIES_FIELDS,
                *_QUALITY_FIELDS,
            },
            exclude_none=True,
        )
        if self.business_days_only:
            settings["business_days_only"] = True
        return settings


def _aligned_interval(minutes: int) -> bool:
    """Intervals that fall on the same clock times every hour or every day."""
    if minutes < 60:
        return 60 % minutes == 0
    return minutes % 60 == 0 and 24 % (minutes // 60) == 0


def _duplicates(values: list[Any]) -> list[Any]:
    seen: set[Any] = set()
    repeated = []
    for value in values:
        if value in seen:
            repeated.append(value)
        seen.add(value)
    return repeated


class SeedList(_SeedModel):
    """A watchlist or a group (PRD FR-015): instruments named by symbol."""

    name: str = Field(pattern=r"^[a-z0-9_]+$", max_length=128)
    title: str
    description: str | None = None
    instruments: list[str] = Field(min_length=1)
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> Self:
        repeated = _duplicates(self.instruments)
        if repeated:
            raise ValueError(f"list {self.name}: instruments listed twice: {', '.join(repeated)}")
        return self


class SeedSpreadLeg(_SeedModel):
    symbol: str
    provider: str


class SeedSpread(_SeedModel):
    """A spread between two yield or rate series (PRD FR-014): long minus short."""

    name: str = Field(pattern=r"^[a-z0-9_]+$", max_length=128)
    title: str
    long: SeedSpreadLeg
    short: SeedSpreadLeg
    enabled: bool = True


_SPREAD_ASSET_TYPES = (AssetType.GOVERNMENT_BOND_YIELD, AssetType.INTEREST_RATE)


class SeedFile(_SeedModel):
    exchanges: list[SeedExchange] = Field(default_factory=list)
    providers: list[SeedProvider] = Field(default_factory=list)
    instruments: list[SeedInstrument] = Field(default_factory=list)
    indicators: list[SeedIndicator] = Field(default_factory=list)
    jobs: list[SeedJob] = Field(default_factory=list)
    watchlists: list[SeedList] = Field(default_factory=list)
    groups: list[SeedList] = Field(default_factory=list)
    spreads: list[SeedSpread] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references_and_duplicates(self) -> Self:
        problems: list[str] = []

        exchange_codes = [e.code for e in self.exchanges]
        provider_names = [p.name for p in self.providers]
        instrument_keys = [(i.asset_type.value, i.symbol, i.exchange) for i in self.instruments]
        indicator_codes = [i.code for i in self.indicators]
        for label, values in (
            ("exchange", exchange_codes),
            ("provider", provider_names),
            ("instrument", instrument_keys),
            ("indicator", indicator_codes),
            ("job", [job.name for job in self.jobs]),
        ):
            problems += [f"duplicate {label}: {value}" for value in _duplicates(values)]

        for instrument in self.instruments:
            if instrument.exchange is not None and instrument.exchange not in exchange_codes:
                problems.append(
                    f"instrument {instrument.symbol}: unknown exchange {instrument.exchange}"
                )
            for provider in instrument.providers:
                if provider not in provider_names:
                    problems.append(f"instrument {instrument.symbol}: unknown provider {provider}")
        for indicator in self.indicators:
            if indicator.provider not in provider_names:
                problems.append(
                    f"indicator {indicator.code}: unknown provider {indicator.provider}"
                )
        for job in self.jobs:
            if job.provider is not None and job.provider not in provider_names:
                problems.append(f"job {job.name}: unknown provider {job.provider}")

        by_symbol: dict[str, list[SeedInstrument]] = {}
        for instrument in self.instruments:
            by_symbol.setdefault(instrument.symbol, []).append(instrument)
        problems += [
            f"duplicate list: {name}"
            for name in _duplicates([lst.name for lst in (*self.watchlists, *self.groups)])
        ]
        problems += [
            f"duplicate spread: {name}" for name in _duplicates([s.name for s in self.spreads])
        ]
        for lst in (*self.watchlists, *self.groups):
            for symbol in lst.instruments:
                matches = by_symbol.get(symbol, [])
                if len(matches) != 1:
                    problems.append(
                        f"list {lst.name}: {symbol} matches {len(matches)} instruments, not one"
                    )
        for spread in self.spreads:
            for leg_name, leg in (("long", spread.long), ("short", spread.short)):
                matches = by_symbol.get(leg.symbol, [])
                if len(matches) != 1:
                    problems.append(f"spread {spread.name}: unknown {leg_name} {leg.symbol}")
                    continue
                instrument = matches[0]
                if instrument.asset_type not in _SPREAD_ASSET_TYPES:
                    problems.append(
                        f"spread {spread.name}: {leg.symbol} is not a yield or interest rate"
                    )
                if leg.provider not in instrument.providers:
                    problems.append(
                        f"spread {spread.name}: {leg.symbol} has no provider {leg.provider}"
                    )

        if problems:
            raise ValueError("; ".join(problems))
        return self
