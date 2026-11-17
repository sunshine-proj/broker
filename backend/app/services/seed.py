"""Loads the seed file and applies it to the database.

The seed file is the source of truth for exchanges, providers, instruments,
provider mappings and economic indicators (PRD FR-004). Applying it is
idempotent: entries are matched on their natural key and updated in place.
Entries removed from the file are left in the database, because historical
observations refer to them; set `enabled = false` to stop tracking something.
"""

import logging
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.repositories.interfaces import (
    CollectionJobRepository,
    EconomicDataRepository,
    InstrumentRepository,
    ListRepository,
    ProviderRepository,
)
from app.schemas.seed import SeedFile
from app.services.budget import BudgetExceededError, check_budget

logger = logging.getLogger(__name__)


class SeedError(Exception):
    """The seed file is missing or invalid."""


@dataclass(frozen=True)
class SeedSummary:
    exchanges: int = 0
    providers: int = 0
    instruments: int = 0
    mappings: int = 0
    indicators: int = 0
    jobs: int = 0
    lists: int = 0
    spreads: int = 0


def load_seed_file(path: Path) -> SeedFile:
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError as exc:
        raise SeedError(f"Seed file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise SeedError(f"Seed file {path} is not valid TOML: {exc}") from exc
    try:
        return SeedFile.model_validate(data)
    except ValidationError as exc:
        raise SeedError(f"Seed file {path} is invalid: {exc}") from exc


async def apply_seed(
    seed: SeedFile,
    *,
    providers: ProviderRepository,
    instruments: InstrumentRepository,
    economic_data: EconomicDataRepository,
    jobs: CollectionJobRepository,
    lists: ListRepository | None = None,
) -> SeedSummary:
    """Create or update everything in `seed`. The caller commits.

    Raises SeedError, before writing anything, if the enabled jobs would use
    more than a provider's rate limits allow (PRD FR-011).
    """
    if lists is None and (seed.watchlists or seed.groups or seed.spreads):
        raise SeedError("watchlists, groups and spreads need a list repository")
    try:
        usages = check_budget(seed)
    except BudgetExceededError as exc:
        raise SeedError(f"Seed file exceeds a provider's rate budget: {exc}") from exc
    for usage in usages:
        logger.info(
            "Provider budget",
            extra={
                "provider": usage.provider,
                "period": usage.period.value,
                "planned": round(usage.planned, 1),
                "limit": usage.limit,
            },
        )
    provider_ids = {
        provider.name: await providers.upsert(
            name=provider.name,
            base_url=provider.base_url,
            enabled=provider.enabled,
            configuration=provider.configuration.model_dump(mode="json"),
        )
        for provider in seed.providers
    }
    exchange_ids = {
        exchange.code: await instruments.upsert_exchange(
            code=exchange.code,
            name=exchange.name,
            country=exchange.country,
            timezone=exchange.timezone,
            trading_open=exchange.trading_open,
            trading_close=exchange.trading_close,
        )
        for exchange in seed.exchanges
    }

    mappings = 0
    instrument_ids: dict[str, uuid.UUID] = {}
    for instrument in seed.instruments:
        instrument_id = instrument_ids[instrument.symbol] = await instruments.upsert(
            symbol=instrument.symbol,
            name=instrument.name,
            asset_type=instrument.asset_type,
            exchange_id=exchange_ids[instrument.exchange] if instrument.exchange else None,
            currency=instrument.currency,
            enabled=instrument.enabled,
            metadata=instrument.metadata,
        )
        for provider_name, provider_symbol in instrument.providers.items():
            await instruments.upsert_mapping(
                instrument_id=instrument_id,
                provider_id=provider_ids[provider_name],
                provider_symbol=provider_symbol,
            )
            mappings += 1

    for indicator in seed.indicators:
        await economic_data.upsert_indicator(
            code=indicator.code,
            name=indicator.name,
            country=indicator.country,
            unit=indicator.unit,
            frequency=indicator.frequency,
            provider_id=provider_ids[indicator.provider],
            provider_series_code=indicator.provider_series_code,
            enabled=indicator.enabled,
            metadata=indicator.metadata,
        )

    for job in seed.jobs:
        await jobs.upsert(
            name=job.name,
            provider_id=provider_ids[job.provider] if job.provider else None,
            kind=job.kind,
            interval_minutes=job.interval_minutes,
            daily_at=job.daily_at,
            market_hours_only=job.market_hours_only,
            retry_minutes=job.retry_minutes,
            settings=job.settings(),
            enabled=job.enabled,
        )
    # Jobs are scheduled only from the seed file: one removed from it stops running.
    disabled = await jobs.disable_others([job.name for job in seed.jobs])
    if disabled:
        logger.info("Jobs no longer in the seed file disabled", extra={"count": disabled})

    if lists is not None:
        for kind, entries in (("watchlist", seed.watchlists), ("group", seed.groups)):
            for entry in entries:
                await lists.upsert_list(
                    kind=kind,
                    name=entry.name,
                    title=entry.title,
                    description=entry.description,
                    enabled=entry.enabled,
                    instrument_ids=[instrument_ids[symbol] for symbol in entry.instruments],
                )
        for spread in seed.spreads:
            await lists.upsert_spread(
                name=spread.name,
                title=spread.title,
                long=(instrument_ids[spread.long.symbol], provider_ids[spread.long.provider]),
                short=(instrument_ids[spread.short.symbol], provider_ids[spread.short.provider]),
                enabled=spread.enabled,
            )
        await lists.disable_others(
            [entry.name for entry in (*seed.watchlists, *seed.groups)],
            [spread.name for spread in seed.spreads],
        )

    summary = SeedSummary(
        exchanges=len(seed.exchanges),
        providers=len(seed.providers),
        instruments=len(seed.instruments),
        mappings=mappings,
        indicators=len(seed.indicators),
        jobs=len(seed.jobs),
        lists=len(seed.watchlists) + len(seed.groups),
        spreads=len(seed.spreads),
    )
    logger.info("Seed applied", extra=summary.__dict__)
    return summary
