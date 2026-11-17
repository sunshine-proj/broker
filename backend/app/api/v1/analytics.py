"""Analytics, watchlists, groups and portfolios (PRD FR-014 to FR-016). Read-only."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query

from app.api.v1.params import (
    END,
    ERRORS,
    START,
    Analytics,
    Paging,
    ProviderFilter,
    Repository,
    time_range,
)
from app.models.enums import AssetType, ObservationType
from app.schemas.analytics import (
    DailyCloseOut,
    GroupOut,
    PerformanceOut,
    PeriodReturnOut,
    PortfolioOut,
    PortfolioSummaryOut,
    PortfolioValueOut,
    RelativePointOut,
    SpreadOut,
    SpreadValueOut,
    WatchlistOut,
    YieldCurvePointOut,
)
from app.schemas.api import Page
from app.services import analytics

router = APIRouter()

Currency = Annotated[
    str | None,
    Query(
        pattern=r"^[A-Za-z0-9]{3,10}$",
        description="Also give each close converted into this currency, e.g. EUR",
    ),
]
ListName = Annotated[
    str | None, Query(max_length=128, description="A watchlist or group: its members only")
]
Name = Annotated[str, Path(max_length=128)]
TypeFilter = Annotated[
    ObservationType | None, Query(description="Default: the instrument's main value")
]


def _upper(value: str | None) -> str | None:
    return value.upper() if value else None


# --- Performance ----------------------------------------------------------------------------


@router.get(
    "/analytics/performance",
    response_model=Page[PerformanceOut],
    responses=ERRORS,
    tags=["analytics"],
    summary="Latest performance of each series",
)
async def performance(
    repo: Repository,
    calc: Analytics,
    page: Paging,
    symbol: Annotated[list[str] | None, Query(description="Repeatable")] = None,
    asset_type: Annotated[list[AssetType] | None, Query()] = None,
    provider: ProviderFilter = None,
    list: ListName = None,  # noqa: A002  (the query parameter's name)
    currency: Currency = None,
) -> Page[PerformanceOut]:
    """Each series' latest daily close, its returns over 1 week, 1 month, 3 months,
    the year to date and 1 year, moving averages and volatility (PRD FR-014).

    Each provider's series is calculated on its own. Returns are fractions; for
    values in percent they are changes in percentage points (`measured_in`).
    """
    return await analytics.performance(
        repo,
        calc,
        symbols=symbol,
        asset_types=asset_type,
        provider=provider,
        list_name=list,
        currency=_upper(currency),
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/analytics/daily",
    response_model=Page[DailyCloseOut],
    responses=ERRORS,
    tags=["analytics"],
    summary="Daily closes with returns, moving averages and volatility",
)
async def daily(
    repo: Repository,
    calc: Analytics,
    page: Paging,
    instrument_id: uuid.UUID,
    observation_type: TypeFilter = None,
    provider: ProviderFilter = None,
    currency: Currency = None,
    start: datetime | None = START,
    end: datetime | None = END,
) -> Page[DailyCloseOut]:
    """One close per UTC day and provider, oldest first. Default range: 1 year.

    The close is the provider's daily candle where there is one, else the
    day's last value. With `currency`, the close converted at that day's rate.
    """
    window = time_range(start, end, default_days=365)
    return await analytics.daily(
        repo,
        calc,
        instrument_id=instrument_id,
        observation_type=observation_type,
        provider=provider,
        currency=_upper(currency),
        start=window.start,
        end=window.end,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/analytics/returns",
    response_model=Page[PeriodReturnOut],
    responses=ERRORS,
    tags=["analytics"],
    summary="Weekly or monthly returns",
)
async def period_returns(
    repo: Repository,
    calc: Analytics,
    page: Paging,
    instrument_id: uuid.UUID,
    period: Literal["week", "month"] = "month",
    observation_type: TypeFilter = None,
    provider: ProviderFilter = None,
    start: datetime | None = START,
    end: datetime | None = END,
) -> Page[PeriodReturnOut]:
    """Returns from each period's last close, by period start, oldest first.

    Weeks start on Monday (UTC). Default range: 2 years of period starts.
    """
    window = time_range(start, end, default_days=730)
    return await analytics.period_returns(
        repo,
        calc,
        instrument_id=instrument_id,
        period=period,
        observation_type=observation_type,
        provider=provider,
        start=window.start,
        end=window.end,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/analytics/relative",
    response_model=Page[RelativePointOut],
    responses=ERRORS,
    tags=["analytics"],
    summary="Relative performance, rebased to 100",
)
async def relative(
    repo: Repository,
    calc: Analytics,
    page: Paging,
    instrument_id: Annotated[list[uuid.UUID] | None, Query(description="Repeatable")] = None,
    list: ListName = None,  # noqa: A002  (the query parameter's name)
    provider: ProviderFilter = None,
    start: datetime | None = START,
    end: datetime | None = END,
) -> Page[RelativePointOut]:
    """Each series' daily closes rebased to 100 at its first close in the range.

    Give instruments, a watchlist or group, or both. Default range: 1 year.
    """
    window = time_range(start, end, default_days=365)
    return await analytics.relative_performance(
        repo,
        calc,
        instrument_ids=instrument_id,
        list_name=list,
        provider=provider,
        start=window.start,
        end=window.end,
        limit=page.limit,
        offset=page.offset,
    )


# --- Spreads and yield curves -----------------------------------------------------------------


@router.get(
    "/analytics/spreads",
    response_model=Page[SpreadOut],
    responses=ERRORS,
    tags=["analytics"],
    summary="Yield spreads",
)
async def spreads(calc: Analytics, page: Paging) -> Page[SpreadOut]:
    """The spreads of the seed file with their newest value, long minus short."""
    return await analytics.spreads(calc, limit=page.limit, offset=page.offset)


@router.get(
    "/analytics/spreads/{name}",
    response_model=Page[SpreadValueOut],
    responses=ERRORS,
    tags=["analytics"],
    summary="Yield spread history",
)
async def spread_history(
    name: Name,
    calc: Analytics,
    page: Paging,
    start: datetime | None = START,
    end: datetime | None = END,
) -> Page[SpreadValueOut]:
    """A spread on the days both series have a value, oldest first. Default: 1 year."""
    window = time_range(start, end, default_days=365)
    return await analytics.spread_history(
        calc,
        name=name,
        start=window.start,
        end=window.end,
        limit=page.limit,
        offset=page.offset,
    )


@router.get(
    "/analytics/yield-curves",
    response_model=Page[YieldCurvePointOut],
    responses=ERRORS,
    tags=["analytics"],
    summary="Yield curves",
)
async def yield_curves(
    calc: Analytics,
    page: Paging,
    curve: Annotated[str | None, Query(max_length=128, description="e.g. US Treasury")] = None,
) -> Page[YieldCurvePointOut]:
    """Each curve's latest yields by maturity, with a week, a month and a year earlier."""
    return await analytics.yield_curves(calc, curve=curve, limit=page.limit, offset=page.offset)


# --- Watchlists and groups ------------------------------------------------------------------


@router.get(
    "/watchlists",
    response_model=Page[WatchlistOut],
    responses=ERRORS,
    tags=["watchlists and groups"],
    summary="Watchlists",
)
async def watchlists(calc: Analytics, page: Paging) -> Page[WatchlistOut]:
    """The watchlists of the seed file, with their instruments in order.

    Their performance: /analytics/performance?list=NAME.
    """
    return await analytics.watchlists(calc, limit=page.limit, offset=page.offset)


@router.get(
    "/watchlists/{name}",
    response_model=WatchlistOut,
    responses=ERRORS,
    tags=["watchlists and groups"],
    summary="Watchlist",
)
async def watchlist(name: Name, calc: Analytics) -> WatchlistOut:
    return await analytics.watchlist(calc, name)


@router.get(
    "/groups",
    response_model=Page[GroupOut],
    responses=ERRORS,
    tags=["watchlists and groups"],
    summary="Instrument groups",
)
async def groups(calc: Analytics, page: Paging) -> Page[GroupOut]:
    """The groups of the seed file, with their members and equal-weighted performance."""
    return await analytics.groups(calc, limit=page.limit, offset=page.offset)


@router.get(
    "/groups/{name}",
    response_model=GroupOut,
    responses=ERRORS,
    tags=["watchlists and groups"],
    summary="Instrument group",
)
async def group(name: Name, calc: Analytics) -> GroupOut:
    """A group's members and equal-weighted performance over each horizon.

    Each member counts once, with its main series from the provider with the
    most daily closes.
    """
    return await analytics.group(calc, name)


# --- Portfolios -----------------------------------------------------------------------------


@router.get(
    "/portfolios",
    response_model=Page[PortfolioSummaryOut],
    responses=ERRORS,
    tags=["portfolios"],
    summary="Portfolios",
)
async def portfolios(calc: Analytics, page: Paging) -> Page[PortfolioSummaryOut]:
    """Portfolios from the portfolio file, valued now in their base currency."""
    return await analytics.portfolios(calc, limit=page.limit, offset=page.offset)


@router.get(
    "/portfolios/{name}",
    response_model=PortfolioOut,
    responses=ERRORS,
    tags=["portfolios"],
    summary="Portfolio holdings",
)
async def portfolio(name: Name, calc: Analytics) -> PortfolioOut:
    """A portfolio's totals and each holding's value, cost, profit and weight."""
    return await analytics.portfolio(calc, name)


@router.get(
    "/portfolios/{name}/history",
    response_model=Page[PortfolioValueOut],
    responses=ERRORS,
    tags=["portfolios"],
    summary="Portfolio value history",
)
async def portfolio_history(
    name: Name,
    calc: Analytics,
    page: Paging,
    start: datetime | None = START,
    end: datetime | None = END,
) -> Page[PortfolioValueOut]:
    """The portfolio's value and cost on every calendar day, oldest first. Default: 1 year."""
    window = time_range(start, end, default_days=365)
    return await analytics.portfolio_history(
        calc,
        name=name,
        start=window.start,
        end=window.end,
        limit=page.limit,
        offset=page.offset,
    )
