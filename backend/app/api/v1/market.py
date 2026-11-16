"""Read endpoints for instruments, market data and economic indicators."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.v1.params import (
    END,
    ERRORS,
    START,
    Paging,
    ProviderFilter,
    Repository,
    time_range,
)
from app.models.enums import (
    AssetType,
    CandleInterval,
    IndicatorFrequency,
    ObservationType,
    QuoteKind,
)
from app.schemas.api import (
    BondYieldOut,
    CandleOut,
    ForexRateOut,
    IndicatorOut,
    IndicatorValueOut,
    InstrumentOut,
    ObservationOut,
    Page,
)
from app.services import read_api

router = APIRouter()


# --- Instruments -----------------------------------------------------------------------------


@router.get(
    "/instruments",
    response_model=Page[InstrumentOut],
    responses=ERRORS,
    tags=["instruments"],
    summary="List instruments",
)
async def list_instruments(
    repo: Repository,
    page: Paging,
    asset_type: AssetType | None = None,
    enabled: bool | None = None,
    provider: ProviderFilter = None,
    search: Annotated[
        str | None, Query(max_length=64, description="Part of the symbol or name")
    ] = None,
) -> Page[InstrumentOut]:
    """Tracked instruments, ordered by asset type and symbol, with each provider's symbol."""
    return await read_api.list_instruments(
        repo,
        asset_type=asset_type,
        enabled=enabled,
        provider=provider,
        search=search,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/instruments/{instrument_id}",
    response_model=InstrumentOut,
    responses=ERRORS,
    tags=["instruments"],
    summary="Instrument details",
)
async def get_instrument(instrument_id: uuid.UUID, repo: Repository) -> InstrumentOut:
    return await read_api.get_instrument(repo, instrument_id)


# --- Prices ------------------------------------------------------------------------------------


@router.get(
    "/prices/latest",
    response_model=Page[ObservationOut],
    responses=ERRORS,
    tags=["prices"],
    summary="Latest prices",
)
async def latest_prices(
    repo: Repository,
    page: Paging,
    asset_type: Annotated[list[AssetType] | None, Query()] = None,
    symbol: Annotated[list[str] | None, Query(description="Instrument symbol; repeatable")] = None,
    instrument_id: Annotated[list[uuid.UUID] | None, Query(description="Repeatable")] = None,
    provider: ProviderFilter = None,
    observation_type: Annotated[
        list[ObservationType] | None,
        Query(description="Default: price, nav, rate, yield and index_level"),
    ] = None,
) -> Page[ObservationOut]:
    """The most recent value of each instrument from each provider.

    Values from different providers are listed separately, never merged.
    """
    return await read_api.latest_prices(
        repo,
        asset_types=asset_type,
        symbols=symbol,
        instrument_ids=instrument_id,
        provider=provider,
        observation_types=observation_type,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/prices/history",
    response_model=Page[ObservationOut],
    responses=ERRORS,
    tags=["prices"],
    summary="Historical prices",
)
async def price_history(
    repo: Repository,
    page: Paging,
    instrument_id: uuid.UUID,
    observation_type: Annotated[
        ObservationType | None,
        Query(description="Default: the instrument's main value (price, rate, yield...)"),
    ] = None,
    provider: ProviderFilter = None,
    start: datetime | None = START,
    end: datetime | None = END,
) -> Page[ObservationOut]:
    """Values of one instrument, oldest first. Default range: the last 30 days."""
    window = time_range(start, end, default_days=30)
    return await read_api.price_history(
        repo,
        instrument_id=instrument_id,
        observation_type=observation_type,
        provider=provider,
        start=window.start,
        end=window.end,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/ohlcv",
    response_model=Page[CandleOut],
    responses=ERRORS,
    tags=["prices"],
    summary="OHLCV candles",
)
async def ohlcv(
    repo: Repository,
    page: Paging,
    instrument_id: uuid.UUID,
    interval: CandleInterval = CandleInterval.DAY_1,
    provider: ProviderFilter = None,
    start: datetime | None = START,
    end: datetime | None = END,
) -> Page[CandleOut]:
    """Closed candles of one instrument, oldest first, by open time. Default range: 90 days."""
    window = time_range(start, end, default_days=90)
    return await read_api.candles(
        repo,
        instrument_id=instrument_id,
        interval=interval,
        provider=provider,
        start=window.start,
        end=window.end,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/forex/rates",
    response_model=Page[ForexRateOut],
    responses=ERRORS,
    tags=["prices"],
    summary="Currency exchange rates",
)
async def forex_rates(
    repo: Repository,
    page: Paging,
    base: Annotated[str | None, Query(min_length=3, max_length=3, description="e.g. EUR")] = None,
    quote: Annotated[str | None, Query(min_length=3, max_length=3, description="e.g. USD")] = None,
    provider: ProviderFilter = None,
    quote_kind: Annotated[
        QuoteKind | None,
        Query(description="`reference` for official fixings, `indicative` for market rates"),
    ] = None,
) -> Page[ForexRateOut]:
    """The latest rate of each currency pair from each provider, in units of quote per base."""
    return await read_api.forex_rates(
        repo,
        base=base,
        quote=quote,
        provider=provider,
        quote_kind=quote_kind,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/bonds/yields",
    response_model=Page[BondYieldOut],
    responses=ERRORS,
    tags=["prices"],
    summary="Government bond yields",
)
async def bond_yields(
    repo: Repository,
    page: Paging,
    country: Annotated[str | None, Query(min_length=2, max_length=2, description="e.g. US")] = None,
    provider: ProviderFilter = None,
) -> Page[BondYieldOut]:
    """The latest yield of each government benchmark, in percent. History: /prices/history."""
    return await read_api.bond_yields(
        repo, country=country, provider=provider, limit=page.limit, offset=page.offset
    )


# --- Economic indicators ---------------------------------------------------------------------


@router.get(
    "/indicators",
    response_model=Page[IndicatorOut],
    responses=ERRORS,
    tags=["indicators"],
    summary="Economic indicators",
)
async def list_indicators(
    repo: Repository,
    page: Paging,
    country: Annotated[str | None, Query(min_length=2, max_length=2)] = None,
    provider: ProviderFilter = None,
    frequency: IndicatorFrequency | None = None,
    enabled: bool | None = None,
) -> Page[IndicatorOut]:
    """Indicators with the current value of their most recent period."""
    return await read_api.list_indicators(
        repo,
        country=country,
        provider=provider,
        frequency=frequency,
        enabled=enabled,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/indicators/{code}/observations",
    response_model=Page[IndicatorValueOut],
    responses=ERRORS,
    tags=["indicators"],
    summary="Indicator history",
)
async def indicator_observations(
    code: str,
    repo: Repository,
    page: Paging,
    start: datetime | None = START,
    end: datetime | None = END,
    include_revisions: Annotated[
        bool, Query(description="Return every stored value, not only the current one")
    ] = False,
) -> Page[IndicatorValueOut]:
    """Values by period, oldest first. Periods are dated by their first day.

    Default range: the last 10 years. By default each period's current value
    is returned; with `include_revisions`, every value stored for it, oldest
    revision first.
    """
    window = time_range(start, end, default_days=3653)
    return await read_api.indicator_observations(
        repo,
        code=code,
        start=window.start,
        end=window.end,
        include_revisions=include_revisions,
        limit=page.limit,
        offset=page.offset,
    )
