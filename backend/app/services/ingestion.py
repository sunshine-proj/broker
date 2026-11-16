"""Ingestion: validates normalized provider data and persists it.

This is the only path from a provider adapter to the database. It resolves
each provider symbol to a tracked instrument, rejects what fails validation
(logged, never corrected), and hands the rest to the repositories, which apply
the deduplication and revision rules. The caller commits.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.repositories.interfaces import (
    EconomicDataRepository,
    InstrumentRepository,
    MarketDataRepository,
)
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
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._instruments = instruments
        self._market_data = market_data
        self._economic_data = economic_data
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

    @staticmethod
    def _reject(kind: str, provider_id: uuid.UUID, symbol: str, reason: str | None) -> None:
        logger.warning(
            "Rejected %s",
            kind,
            extra={"provider_id": str(provider_id), "symbol": symbol, "reason": reason},
        )
