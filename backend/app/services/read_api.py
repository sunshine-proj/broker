"""The read API's business rules: defaults, validation and response shaping.

Routers parse the request and call these functions; the queries themselves
are in app/repositories/read_queries.py.
"""

import uuid
from collections.abc import Callable, Collection
from datetime import datetime
from http import HTTPStatus
from typing import Any

from pydantic import BaseModel

from app.api.errors import AppError
from app.models.enums import (
    AssetType,
    CandleInterval,
    CollectorStatus,
    IndicatorFrequency,
    IssueKind,
    IssueStatus,
    ObservationType,
    QuoteKind,
)
from app.repositories.read_queries import Row, SqlReadRepository
from app.schemas.api import (
    BondYieldOut,
    CandleOut,
    ExecutionOut,
    ForexRateOut,
    IndicatorOut,
    IndicatorValueOut,
    InstrumentOut,
    JobStatusOut,
    ObservationOut,
    Page,
    ProviderOut,
    QualityIssueOut,
    RateLimitOut,
)
from app.schemas.provider_config import ProviderSettings

# What /prices returns unless asked for other observation types.
PRICE_TYPES = (
    ObservationType.PRICE,
    ObservationType.NAV,
    ObservationType.RATE,
    ObservationType.YIELD,
    ObservationType.INDEX_LEVEL,
)

# The observation type an instrument's history defaults to.
PRIMARY_TYPE = {
    AssetType.STOCK: ObservationType.PRICE,
    AssetType.ETF: ObservationType.PRICE,
    AssetType.CRYPTO: ObservationType.PRICE,
    AssetType.COMMODITY: ObservationType.PRICE,
    AssetType.INDEX: ObservationType.INDEX_LEVEL,
    AssetType.FOREX: ObservationType.RATE,
    AssetType.INTEREST_RATE: ObservationType.RATE,
    AssetType.GOVERNMENT_BOND_YIELD: ObservationType.YIELD,
}


def _page[T: BaseModel](
    rows: list[Row], limit: int, offset: int, convert: Callable[[Row], T]
) -> Page[T]:
    """Rows were fetched with limit + 1; the extra row only says another page exists."""
    return Page[T](
        items=[convert(row) for row in rows[:limit]],
        limit=limit,
        offset=offset,
        has_more=len(rows) > limit,
    )


async def _check_provider(repository: SqlReadRepository, provider: str | None) -> None:
    if provider is not None and not await repository.provider_exists(provider):
        raise AppError(
            "unknown_provider",
            f"No provider named {provider!r}.",
            HTTPStatus.UNPROCESSABLE_ENTITY,
            {"provider": provider},
        )


async def _instrument(repository: SqlReadRepository, instrument_id: uuid.UUID) -> Row:
    row = await repository.get_instrument(instrument_id)
    if row is None:
        raise AppError(
            "instrument_not_found",
            f"No instrument with id {instrument_id}.",
            HTTPStatus.NOT_FOUND,
        )
    return row


# --- Instruments -----------------------------------------------------------------------------


async def list_instruments(
    repository: SqlReadRepository,
    *,
    asset_type: AssetType | None,
    enabled: bool | None,
    provider: str | None,
    search: str | None,
    limit: int,
    offset: int,
) -> Page[InstrumentOut]:
    await _check_provider(repository, provider)
    rows = await repository.list_instruments(
        asset_type=asset_type,
        enabled=enabled,
        provider=provider,
        search=search,
        limit=limit + 1,
        offset=offset,
    )
    return _page(rows, limit, offset, InstrumentOut.model_validate)


async def get_instrument(repository: SqlReadRepository, instrument_id: uuid.UUID) -> InstrumentOut:
    return InstrumentOut.model_validate(await _instrument(repository, instrument_id))


# --- Prices -----------------------------------------------------------------------------------


async def latest_prices(
    repository: SqlReadRepository,
    *,
    asset_types: Collection[AssetType] | None,
    symbols: Collection[str] | None,
    instrument_ids: Collection[uuid.UUID] | None,
    provider: str | None,
    observation_types: Collection[ObservationType] | None,
    limit: int,
    offset: int,
) -> Page[ObservationOut]:
    await _check_provider(repository, provider)
    rows = await repository.latest_observations(
        observation_types=observation_types or PRICE_TYPES,
        asset_types=asset_types,
        symbols=symbols,
        instrument_ids=instrument_ids,
        provider=provider,
        limit=limit + 1,
        offset=offset,
    )
    return _page(rows, limit, offset, ObservationOut.model_validate)


async def price_history(
    repository: SqlReadRepository,
    *,
    instrument_id: uuid.UUID,
    observation_type: ObservationType | None,
    provider: str | None,
    start: datetime,
    end: datetime,
    limit: int,
    offset: int,
) -> Page[ObservationOut]:
    instrument = await _instrument(repository, instrument_id)
    await _check_provider(repository, provider)
    rows = await repository.observation_history(
        instrument_id=instrument_id,
        observation_type=observation_type or PRIMARY_TYPE[instrument["asset_type"]],
        start=start,
        end=end,
        provider=provider,
        limit=limit + 1,
        offset=offset,
    )
    return _page(rows, limit, offset, ObservationOut.model_validate)


async def candles(
    repository: SqlReadRepository,
    *,
    instrument_id: uuid.UUID,
    interval: CandleInterval,
    provider: str | None,
    start: datetime,
    end: datetime,
    limit: int,
    offset: int,
) -> Page[CandleOut]:
    await _instrument(repository, instrument_id)
    await _check_provider(repository, provider)
    rows = await repository.candles(
        instrument_id=instrument_id,
        interval=interval,
        start=start,
        end=end,
        provider=provider,
        limit=limit + 1,
        offset=offset,
    )
    return _page(rows, limit, offset, CandleOut.model_validate)


async def forex_rates(
    repository: SqlReadRepository,
    *,
    base: str | None,
    quote: str | None,
    provider: str | None,
    quote_kind: QuoteKind | None,
    limit: int,
    offset: int,
) -> Page[ForexRateOut]:
    await _check_provider(repository, provider)
    rows = await repository.latest_observations(
        observation_types=[ObservationType.RATE],
        asset_types=[AssetType.FOREX],
        provider=provider,
        quote_kind=quote_kind,
        base_currency=base.upper() if base else None,
        quote_currency=quote.upper() if quote else None,
        limit=limit + 1,
        offset=offset,
    )

    def convert(row: Row) -> ForexRateOut:
        base_currency, _, quote_currency = row["symbol"].partition("/")
        return ForexRateOut.model_validate(
            {**row, "base_currency": base_currency, "quote_currency": quote_currency}
        )

    return _page(rows, limit, offset, convert)


async def bond_yields(
    repository: SqlReadRepository,
    *,
    country: str | None,
    provider: str | None,
    limit: int,
    offset: int,
) -> Page[BondYieldOut]:
    await _check_provider(repository, provider)
    rows = await repository.latest_observations(
        observation_types=[ObservationType.YIELD],
        asset_types=[AssetType.GOVERNMENT_BOND_YIELD],
        provider=provider,
        country=country.upper() if country else None,
        limit=limit + 1,
        offset=offset,
    )

    def convert(row: Row) -> BondYieldOut:
        metadata = row["instrument_metadata"] or {}
        return BondYieldOut.model_validate(
            {**row, "country": metadata.get("country"), "maturity": metadata.get("maturity")}
        )

    return _page(rows, limit, offset, convert)


# --- Economic indicators ----------------------------------------------------------------------


async def list_indicators(
    repository: SqlReadRepository,
    *,
    country: str | None,
    provider: str | None,
    frequency: IndicatorFrequency | None,
    enabled: bool | None,
    limit: int,
    offset: int,
) -> Page[IndicatorOut]:
    await _check_provider(repository, provider)
    rows = await repository.indicators(
        country=country.upper() if country else None,
        provider=provider,
        frequency=frequency,
        enabled=enabled,
        limit=limit + 1,
        offset=offset,
    )

    def convert(row: Row) -> IndicatorOut:
        latest = None
        if row["latest_observed_at"] is not None:
            latest = IndicatorValueOut(
                observed_at=row["latest_observed_at"],
                ingested_at=row["latest_ingested_at"],
                value=row["latest_value"],
                metadata=row["latest_metadata"],
            )
        return IndicatorOut.model_validate({**row, "latest": latest})

    return _page(rows, limit, offset, convert)


async def indicator_observations(
    repository: SqlReadRepository,
    *,
    code: str,
    start: datetime,
    end: datetime,
    include_revisions: bool,
    limit: int,
    offset: int,
) -> Page[IndicatorValueOut]:
    indicator_id = await repository.indicator_id(code)
    if indicator_id is None:
        raise AppError(
            "indicator_not_found", f"No indicator with code {code!r}.", HTTPStatus.NOT_FOUND
        )
    rows = await repository.indicator_observations(
        indicator_id=indicator_id,
        start=start,
        end=end,
        include_revisions=include_revisions,
        limit=limit + 1,
        offset=offset,
    )
    return _page(rows, limit, offset, IndicatorValueOut.model_validate)


# --- Providers and collection ------------------------------------------------------------------


async def list_providers(
    repository: SqlReadRepository, *, limit: int, offset: int
) -> Page[ProviderOut]:
    rows = await repository.providers(limit=limit + 1, offset=offset)

    def convert(row: Row) -> ProviderOut:
        # Only the rate limits are shown from the configuration.
        settings = ProviderSettings.model_validate(row["configuration"] or {})
        limits = [
            RateLimitOut(limit=rate.limit, period=rate.period.value)
            for rate in settings.rate_limits
        ]
        fields: dict[str, Any] = {k: v for k, v in row.items() if k != "configuration"}
        return ProviderOut.model_validate({**fields, "rate_limits": limits})

    return _page(rows, limit, offset, convert)


async def job_status(
    repository: SqlReadRepository,
    *,
    provider: str | None,
    enabled: bool | None,
    limit: int,
    offset: int,
) -> Page[JobStatusOut]:
    await _check_provider(repository, provider)
    rows = await repository.jobs(provider=provider, enabled=enabled, limit=limit + 1, offset=offset)
    return _page(rows, limit, offset, JobStatusOut.model_validate)


async def executions(
    repository: SqlReadRepository,
    *,
    job: str | None,
    status: CollectorStatus | None,
    limit: int,
    offset: int,
) -> Page[ExecutionOut]:
    rows = await repository.executions(job=job, status=status, limit=limit + 1, offset=offset)
    return _page(rows, limit, offset, ExecutionOut.model_validate)


async def quality_issues(
    repository: SqlReadRepository,
    *,
    kind: IssueKind | None,
    status: IssueStatus | None,
    provider: str | None,
    symbol: str | None,
    start: datetime,
    end: datetime,
    limit: int,
    offset: int,
) -> Page[QualityIssueOut]:
    await _check_provider(repository, provider)
    rows = await repository.quality_issues(
        kind=kind,
        status=status,
        provider=provider,
        symbol=symbol,
        start=start,
        end=end,
        limit=limit + 1,
        offset=offset,
    )
    return _page(rows, limit, offset, QualityIssueOut.model_validate)
