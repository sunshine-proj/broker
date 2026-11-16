"""One collection run for one provider: fetch, validate, store.

Provider failures are contained and recorded as provider health; they never
propagate to the caller. Scheduling and execution history are added by the
collection engine (roadmap Phase 5), which calls these functions.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from app.models import Provider as ProviderRow
from app.models.enums import CandleInterval
from app.providers.factory import ProviderFactory
from app.repositories.interfaces import InstrumentRepository
from app.schemas.normalized import Candle, Quote
from app.services.ingestion import IngestionResult, IngestionService
from app.services.provider_health import ProviderHealthService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CollectionOutcome:
    provider: str
    # Symbols requested from the provider.
    requested: int
    # False when at least one provider call failed.
    succeeded: bool
    result: IngestionResult


def _combine(results: list[IngestionResult]) -> IngestionResult:
    return IngestionResult(
        received=sum(r.received for r in results),
        written=sum(r.written for r in results),
        unchanged=sum(r.unchanged for r in results),
        conflicting=sum(r.conflicting for r in results),
        rejected=sum(r.rejected for r in results),
    )


class CollectionService:
    def __init__(
        self,
        *,
        factory: ProviderFactory,
        instruments: InstrumentRepository,
        ingestion: IngestionService,
        health: ProviderHealthService,
    ) -> None:
        self._factory = factory
        self._instruments = instruments
        self._ingestion = ingestion
        self._health = health

    async def _symbols(self, provider: ProviderRow) -> list[str]:
        mappings = await self._instruments.list_mappings(provider.id)
        return [mapping.provider_symbol for mapping in mappings]

    async def collect_quotes(self, provider: ProviderRow) -> CollectionOutcome:
        """Fetch and store the latest value of every enabled instrument mapped to the provider."""
        symbols = await self._symbols(provider)
        if not symbols:
            return CollectionOutcome(provider.name, 0, True, IngestionResult())

        async def fetch() -> list[Quote]:
            return await self._factory.build(provider).fetch_quotes(symbols)

        quotes = await self._health.guard(provider.id, fetch)
        if quotes is None:
            return CollectionOutcome(provider.name, len(symbols), False, IngestionResult())
        result = await self._ingestion.ingest_quotes(provider.id, quotes)
        logger.info("Quotes collected", extra={"provider": provider.name, **result.__dict__})
        return CollectionOutcome(provider.name, len(symbols), True, result)

    async def collect_candles(
        self, provider: ProviderRow, interval: CandleInterval, start: datetime, end: datetime
    ) -> CollectionOutcome:
        """Fetch and store closed candles of every enabled instrument mapped to the provider.

        A symbol whose request fails is skipped; the remaining symbols are still collected.
        """
        symbols = await self._symbols(provider)
        results: list[IngestionResult] = []
        succeeded = True

        for symbol in symbols:

            async def fetch(symbol: str = symbol) -> list[Candle]:
                return await self._factory.build(provider).fetch_candles(
                    symbol, interval, start, end
                )

            candles = await self._health.guard(provider.id, fetch)
            if candles is None:
                succeeded = False
                continue
            results.append(await self._ingestion.ingest_candles(provider.id, candles))

        result = _combine(results)
        logger.info(
            "Candles collected",
            extra={"provider": provider.name, "interval": interval.value, **result.__dict__},
        )
        return CollectionOutcome(provider.name, len(symbols), succeeded, result)
