"""Gap backfill (PRD FR-012): re-fetch the ranges the quality checks found missing.

Each run takes a provider's open gap issues, oldest first, up to the job's
`max_requests`, and asks the provider for each range again: candles for candle
gaps, quote history for observation gaps. The issue is then closed:

- `filled` when values now exist in the range;
- `no_data` when the provider had nothing for it (a holiday, for example);
- `unrecoverable` when the provider cannot re-fetch that kind of data, the
  instrument is no longer tracked, or the request failed three times.

Requests go through the provider's rate limiter like any other, and
`max_requests` is counted in the startup budget check (FR-011).
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.models import DataQualityIssue
from app.models import Provider as ProviderRow
from app.models.enums import IssueResolution
from app.providers.base import Capability
from app.providers.factory import ADAPTERS
from app.repositories.interfaces import InstrumentRepository
from app.repositories.quality import SqlQualityRepository
from app.services.collection import CollectionOutcome, CollectionService, combine
from app.services.ingestion import IngestionResult

logger = logging.getLogger(__name__)

# Failed attempts after which a gap is given up as unrecoverable.
MAX_ATTEMPTS = 3

# The known values at either end of a gap are excluded from the request.
_JUST_AFTER = timedelta(microseconds=1)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class BackfillService:
    def __init__(
        self,
        *,
        collection: CollectionService,
        instruments: InstrumentRepository,
        quality: SqlQualityRepository,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._collection = collection
        self._instruments = instruments
        self._quality = quality
        self._clock = clock

    async def run(self, provider: ProviderRow, max_requests: int) -> CollectionOutcome:
        gaps = await self._quality.open_gaps(provider.id, max_requests)
        symbols = {
            mapping.instrument_id: mapping.provider_symbol
            for mapping in await self._instruments.list_mappings(provider.id)
        }
        adapter = ADAPTERS.get(provider.name)
        capabilities = adapter.capabilities if adapter is not None else frozenset()

        results: list[IngestionResult] = []
        errors: list[str] = []
        closed: dict[str, int] = {}
        for gap in gaps:
            outcome = await self._fill(provider, gap, symbols, capabilities)
            if outcome is None:
                closed["unrecoverable"] = closed.get("unrecoverable", 0) + 1
                continue
            results.append(outcome.result)
            if not outcome.succeeded:
                errors += outcome.errors
                await self._failed(gap)
                continue
            resolution = await self._resolve(provider, gap)
            closed[resolution.value] = closed.get(resolution.value, 0) + 1

        logger.info(
            "Backfill finished",
            extra={"provider": provider.name, "gaps": len(gaps), "closed": closed},
        )
        return CollectionOutcome(
            provider.name, len(gaps), not errors, combine(results), tuple(errors)
        )

    async def _fill(
        self,
        provider: ProviderRow,
        gap: DataQualityIssue,
        symbols: dict,
        capabilities: frozenset[Capability],
    ) -> CollectionOutcome | None:
        """Re-fetch one gap; None if it cannot be (the issue is then closed)."""
        symbol = symbols.get(gap.instrument_id)
        needed = Capability.CANDLES if gap.interval is not None else Capability.QUOTE_HISTORY
        reason = None
        if symbol is None:
            reason = "the instrument is no longer tracked by this provider"
        elif needed not in capabilities:
            reason = f"the provider offers no {needed.value.replace('_', ' ')}"
        elif gap.range_start is None or gap.range_end is None:
            reason = "the gap has no range"
        if reason is not None:
            await self._quality.close(
                gap.id, IssueResolution.UNRECOVERABLE, self._clock(), {"reason": reason}
            )
            return None

        assert gap.range_start is not None and gap.range_end is not None
        start, end = gap.range_start + _JUST_AFTER, gap.range_end
        if gap.interval is not None:
            return await self._collection.collect_candles(
                provider, gap.interval, start, end, symbols=[symbol]
            )
        return await self._collection.collect_quote_history(provider, start, end, symbols=[symbol])

    async def _resolve(self, provider: ProviderRow, gap: DataQualityIssue) -> IssueResolution:
        assert gap.instrument_id is not None
        assert gap.range_start is not None and gap.range_end is not None
        found = await self._quality.count_in_range(
            provider.id,
            gap.instrument_id,
            gap.range_start,
            gap.range_end,
            observation_type=gap.observation_type,
            interval=gap.interval,
        )
        resolution = IssueResolution.FILLED if found else IssueResolution.NO_DATA
        await self._quality.close(gap.id, resolution, self._clock(), {"values_found": found})
        return resolution

    async def _failed(self, gap: DataQualityIssue) -> None:
        attempts = int(gap.details.get("attempts", 0)) + 1
        now = self._clock()
        if attempts >= MAX_ATTEMPTS:
            await self._quality.close(
                gap.id,
                IssueResolution.UNRECOVERABLE,
                now,
                {"attempts": attempts, "reason": "the provider failed every attempt"},
            )
        else:
            await self._quality.note(gap.id, now, {"attempts": attempts})
