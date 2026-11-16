"""One collection run for one provider: fetch, validate, store.

Provider failures are contained and recorded as provider health; they never
propagate to the caller. Scheduling and execution history are handled by the
job runner (app/collectors), which calls these functions.
"""

import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.models import Provider as ProviderRow
from app.models.enums import CandleInterval, ObservationType
from app.providers.base import Provider
from app.providers.factory import ProviderFactory
from app.repositories.interfaces import EconomicDataRepository, InstrumentRepository
from app.schemas.normalized import Quote
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
    # Descriptions of the provider failures, if any.
    errors: tuple[str, ...] = field(default=())


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
        economic_data: EconomicDataRepository,
        ingestion: IngestionService,
        health: ProviderHealthService,
    ) -> None:
        self._factory = factory
        self._instruments = instruments
        self._economic_data = economic_data
        self._ingestion = ingestion
        self._health = health

    async def _symbols(self, provider: ProviderRow) -> list[str]:
        mappings = await self._instruments.list_mappings(provider.id)
        return [mapping.provider_symbol for mapping in mappings]

    async def collect_quotes(
        self,
        provider: ProviderRow,
        observation_types: Collection[ObservationType] | None = None,
        *,
        symbols: Sequence[str] | None = None,
    ) -> CollectionOutcome:
        """Fetch and store the latest value of every enabled instrument mapped to the provider.

        `observation_types` limits what is fetched; None means everything the
        provider offers. `symbols` replaces the provider's mapped symbols.
        """
        symbols = list(symbols) if symbols is not None else await self._symbols(provider)
        if not symbols:
            return CollectionOutcome(provider.name, 0, True, IngestionResult())

        async def fetch() -> list[Quote]:
            return await self._factory.build(provider).fetch_quotes(symbols, observation_types)

        errors: list[str] = []
        quotes = await self._health.guard(provider.id, fetch, errors)
        if quotes is None:
            return CollectionOutcome(
                provider.name, len(symbols), False, IngestionResult(), tuple(errors)
            )
        result = await self._ingestion.ingest_quotes(provider.id, quotes)
        logger.info("Quotes collected", extra={"provider": provider.name, **result.__dict__})
        return CollectionOutcome(provider.name, len(symbols), True, result)

    async def collect_candles(
        self,
        provider: ProviderRow,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
        *,
        symbols: Sequence[str] | None = None,
    ) -> CollectionOutcome:
        """Fetch and store closed candles of every enabled instrument mapped to the provider.

        A symbol whose request fails is skipped; the remaining symbols are still collected.
        """

        async def collect(adapter: Provider, symbol: str) -> IngestionResult:
            candles = await adapter.fetch_candles(symbol, interval, start, end)
            return await self._ingestion.ingest_candles(provider.id, candles)

        outcome = await self._per_symbol(provider, collect, symbols)
        logger.info(
            "Candles collected",
            extra={
                "provider": provider.name,
                "interval": interval.value,
                **outcome.result.__dict__,
            },
        )
        return outcome

    async def collect_quote_history(
        self,
        provider: ProviderRow,
        start: datetime,
        end: datetime,
        *,
        symbols: Sequence[str] | None = None,
    ) -> CollectionOutcome:
        """Fetch and store past values of every enabled instrument mapped to the provider.

        A symbol whose request fails is skipped; the remaining symbols are still collected.
        """

        async def collect(adapter: Provider, symbol: str) -> IngestionResult:
            quotes = await adapter.fetch_quote_history(symbol, start, end)
            return await self._ingestion.ingest_quotes(provider.id, quotes)

        outcome = await self._per_symbol(provider, collect, symbols)
        logger.info(
            "Quote history collected", extra={"provider": provider.name, **outcome.result.__dict__}
        )
        return outcome

    async def collect_indicators(
        self, provider: ProviderRow, start: datetime, end: datetime
    ) -> CollectionOutcome:
        """Fetch and store values of every enabled economic indicator from the provider.

        A series whose request fails is skipped; the remaining series are still collected.
        """
        indicators = await self._economic_data.list_indicators(
            provider_id=provider.id, enabled=True, limit=None
        )

        async def collect(adapter: Provider, series_code: str) -> IngestionResult:
            values = await adapter.fetch_indicator(series_code, start, end)
            return await self._ingestion.ingest_indicator_values(provider.id, values)

        outcome = await self._per_symbol(
            provider, collect, [indicator.provider_series_code for indicator in indicators]
        )
        logger.info(
            "Indicators collected", extra={"provider": provider.name, **outcome.result.__dict__}
        )
        return outcome

    async def _per_symbol(
        self,
        provider: ProviderRow,
        collect: Callable[[Provider, str], Awaitable[IngestionResult]],
        symbols: Sequence[str] | None = None,
    ) -> CollectionOutcome:
        """Run `collect` for each symbol, containing each symbol's provider failure.

        `symbols` defaults to the provider's enabled instrument mappings.
        """
        symbols = list(symbols) if symbols is not None else await self._symbols(provider)
        results: list[IngestionResult] = []
        errors: list[str] = []
        succeeded = True

        for symbol in symbols:

            async def run(symbol: str = symbol) -> IngestionResult:
                return await collect(self._factory.build(provider), symbol)

            result = await self._health.guard(provider.id, run, errors)
            if result is None:
                succeeded = False
                continue
            results.append(result)

        return CollectionOutcome(
            provider.name, len(symbols), succeeded, _combine(results), tuple(errors)
        )
