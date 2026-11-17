"""Ingestion: validates normalized provider data and persists it.

This is the only path from a provider adapter to the database. It resolves
each provider symbol to a tracked instrument, rejects what fails validation
(logged, never corrected), and hands the rest to the repositories, which apply
the deduplication and revision rules. A stored observation or candle that the
provider re-sends with a different value is recorded as a data-quality issue,
and the stored observation is marked suspect (PRD FR-012). The caller commits.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.models.enums import CandleInterval, IssueKind, ObservationType
from app.repositories.interfaces import (
    Conflict,
    EconomicDataRepository,
    InstrumentRepository,
    MarketDataRepository,
    WriteResult,
)
from app.repositories.quality import IssueIn, SqlQualityRepository
from app.schemas.market_data import EconomicObservationIn, MarketObservationIn, OHLCVIn
from app.schemas.normalized import Candle, IndicatorValue, Quote
from app.services.validation import validate_candle, validate_indicator_value, validate_quote

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IngestionResult:
    received: int = 0
    # New rows written. For economic data this includes revisions.
    written: int = 0
    # Already stored with the same value.
    unchanged: int = 0
    # Market data only: already stored with a different value, stored value kept.
    conflicting: int = 0
    # Failed validation, or no enabled instrument is mapped to the symbol.
    rejected: int = 0


def _utc_now() -> datetime:
    return datetime.now(UTC)


class IngestionService:
    def __init__(
        self,
        *,
        instruments: InstrumentRepository,
        market_data: MarketDataRepository,
        economic_data: EconomicDataRepository,
        quality: SqlQualityRepository | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._instruments = instruments
        self._market_data = market_data
        self._economic_data = economic_data
        self._quality = quality
        self._clock = clock

    async def ingest_quotes(
        self, provider_id: uuid.UUID, quotes: Sequence[Quote]
    ) -> IngestionResult:
        if not quotes:
            return IngestionResult()
        instruments = await self._instruments.get_instruments_by_provider_symbol(provider_id)
        now = self._clock()

        accepted: list[MarketObservationIn] = []
        for quote in quotes:
            instrument = instruments.get(quote.provider_symbol)
            problem = (
                "no enabled instrument is mapped to this symbol"
                if instrument is None
                else validate_quote(quote, instrument.currency, now)
            )
            if instrument is None or problem is not None:
                self._reject("quote", provider_id, quote.provider_symbol, problem)
                continue
            accepted.append(
                MarketObservationIn(
                    instrument_id=instrument.id,
                    provider_id=provider_id,
                    **quote.model_dump(exclude={"provider_symbol"}),
                )
            )

        result = await self._market_data.add_observations(accepted)
        await self._record_conflicts(result, now, candles=False)
        return IngestionResult(
            received=len(quotes),
            written=result.inserted,
            unchanged=result.unchanged,
            conflicting=result.conflicting,
            rejected=len(quotes) - len(accepted),
        )

    async def ingest_candles(
        self, provider_id: uuid.UUID, candles: Sequence[Candle]
    ) -> IngestionResult:
        if not candles:
            return IngestionResult()
        instruments = await self._instruments.get_instruments_by_provider_symbol(provider_id)
        now = self._clock()

        accepted: list[OHLCVIn] = []
        for candle in candles:
            instrument = instruments.get(candle.provider_symbol)
            problem = (
                "no enabled instrument is mapped to this symbol"
                if instrument is None
                else validate_candle(candle, instrument.currency, now)
            )
            if instrument is None or problem is not None:
                self._reject("candle", provider_id, candle.provider_symbol, problem)
                continue
            accepted.append(
                OHLCVIn(
                    instrument_id=instrument.id,
                    provider_id=provider_id,
                    **candle.model_dump(exclude={"provider_symbol"}),
                )
            )

        result = await self._market_data.add_candles(accepted)
        await self._record_conflicts(result, now, candles=True)
        return IngestionResult(
            received=len(candles),
            written=result.inserted,
            unchanged=result.unchanged,
            conflicting=result.conflicting,
            rejected=len(candles) - len(accepted),
        )

    async def ingest_indicator_values(
        self, provider_id: uuid.UUID, values: Sequence[IndicatorValue]
    ) -> IngestionResult:
        if not values:
            return IngestionResult()
        indicators = {
            indicator.provider_series_code: indicator
            for indicator in await self._economic_data.list_indicators(
                provider_id=provider_id, enabled=True, limit=None
            )
        }
        now = self._clock()

        accepted: list[EconomicObservationIn] = []
        for value in values:
            indicator = indicators.get(value.series_code)
            problem = (
                "no enabled indicator uses this series code"
                if indicator is None
                else validate_indicator_value(value, now)
            )
            if indicator is None or problem is not None:
                self._reject("indicator value", provider_id, value.series_code, problem)
                continue
            accepted.append(
                EconomicObservationIn(
                    indicator_id=indicator.id,
                    observed_at=value.observed_at,
                    value=value.value,
                    metadata=value.metadata,
                )
            )

        result = await self._economic_data.add_observations(accepted, ingested_at=now)
        return IngestionResult(
            received=len(values),
            written=result.inserted + result.revised,
            unchanged=result.unchanged,
            rejected=len(values) - len(accepted),
        )

    async def _record_conflicts(self, result: WriteResult, now: datetime, *, candles: bool) -> None:
        if not result.conflicts:
            return
        if self._quality is not None:
            for conflict in result.conflicts:
                await self._quality.record_event(_conflict_issue(conflict, candles=candles), now)
        if not candles:
            # The provider no longer stands by the stored value.
            await self._market_data.mark_suspect([c.key for c in result.conflicts])

    @staticmethod
    def _reject(kind: str, provider_id: uuid.UUID, symbol: str, reason: str | None) -> None:
        logger.warning(
            "Rejected %s",
            kind,
            extra={"provider_id": str(provider_id), "symbol": symbol, "reason": reason},
        )


def _conflict_issue(conflict: Conflict, *, candles: bool) -> IssueIn:
    key = conflict.key
    if candles:
        what, at = CandleInterval(key["interval"]), key["timestamp"]
        label = f"{what.value} candle"
    else:
        what, at = ObservationType(key["observation_type"]), key["observed_at"]
        label = what.value
    return IssueIn(
        kind=IssueKind.CONFLICT,
        fingerprint=(
            f"conflict:{key['provider_id']}:{key['instrument_id']}:{what.value}:{at.isoformat()}"
        ),
        message=f"The provider re-sent a stored {label} with different values; stored values kept",
        instrument_id=key["instrument_id"],
        provider_id=key["provider_id"],
        observation_type=None if candles else what,  # type: ignore[arg-type]
        interval=what if candles else None,  # type: ignore[arg-type]
        range_start=at,
        details={
            **{f"stored_{name}": value for name, value in conflict.stored.items()},
            **{f"incoming_{name}": value for name, value in conflict.incoming.items()},
        },
    )
