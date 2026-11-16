"""Loads the seed file and applies it to the database.

The seed file is the source of truth for exchanges, providers, instruments,
provider mappings and economic indicators (PRD FR-004). Applying it is
idempotent: entries are matched on their natural key and updated in place.
Entries removed from the file are left in the database, because historical
observations refer to them; set `enabled = false` to stop tracking something.
"""

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.repositories.interfaces import (
    EconomicDataRepository,
    InstrumentRepository,
    ProviderRepository,
)
from app.schemas.seed import SeedFile

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
) -> SeedSummary:
    """Create or update everything in `seed`. The caller commits."""
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
        )
        for exchange in seed.exchanges
    }

    mappings = 0
    for instrument in seed.instruments:
        instrument_id = await instruments.upsert(
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

    summary = SeedSummary(
        exchanges=len(seed.exchanges),
        providers=len(seed.providers),
        instruments=len(seed.instruments),
        mappings=mappings,
        indicators=len(seed.indicators),
    )
    logger.info("Seed applied", extra=summary.__dict__)
    return summary
