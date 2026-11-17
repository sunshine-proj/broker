"""Analytics, watchlists, groups and portfolios for the API (PRD FR-014 to FR-016).

Routers parse the request and call these functions; the calculations are
database views (migration 0009), queried in app/repositories/analytics_queries.py.
"""

import uuid
from collections.abc import Collection
from datetime import datetime
from decimal import Decimal
from http import HTTPStatus
from itertools import groupby

from app.api.errors import AppError
from app.models.enums import AssetType, ObservationType
from app.repositories.analytics_queries import Row, SqlAnalyticsRepository
from app.repositories.read_queries import SqlReadRepository
from app.schemas.analytics import (
    ConvertedOut,
    DailyCloseOut,
    GroupHorizonOut,
    GroupOut,
    HoldingOut,
    HorizonOut,
    ListMemberOut,
    PerformanceOut,
    PeriodReturnOut,
    PortfolioOut,
    PortfolioSummaryOut,
    PortfolioValueOut,
    RelativePointOut,
    SpreadLegOut,
    SpreadOut,
    SpreadValueOut,
    WatchlistOut,
    YieldCurvePointOut,
)
from app.schemas.api import Page
from app.services.read_api import check_provider, instrument_row, to_page

HORIZONS = ("1w", "1m", "3m", "ytd", "1y")
# How many instruments one relative-performance request may compare.
MAX_COMPARED = 50


def _not_found(code: str, message: str) -> AppError:
    return AppError(code, message, HTTPStatus.NOT_FOUND)


def _converted(row: Row, currency: str | None) -> ConvertedOut | None:
    if currency is None:
        return None
    return ConvertedOut(currency=currency, close=row["converted_close"])


# --- Performance ----------------------------------------------------------------------------


async def performance(
    read: SqlReadRepository,
    analytics: SqlAnalyticsRepository,
    *,
    symbols: Collection[str] | None,
    asset_types: Collection[AssetType] | None,
    provider: str | None,
    list_name: str | None,
    currency: str | None,
    limit: int,
    offset: int,
) -> Page[PerformanceOut]:
    await check_provider(read, provider)
    if list_name is not None and await analytics.list_instrument_ids(list_name) is None:
        raise _not_found("list_not_found", f"No watchlist or group named {list_name!r}.")
    rows = await analytics.performance(
        symbols=symbols,
        asset_types=asset_types,
        provider=provider,
        list_name=list_name,
        currency=currency,
    )
    # One row per series and horizon, in series order: one item per series.
    series = [
        list(group)
        for _, group in groupby(
            rows, key=lambda r: (r["instrument_id"], r["provider_id"], r["observation_type"])
        )
    ]
    window = series[offset : offset + limit + 1]
    return Page[PerformanceOut](
        items=[_performance(group, currency) for group in window[:limit]],
        limit=limit,
        offset=offset,
        has_more=len(window) > limit,
    )


def _performance(rows: list[Row], currency: str | None) -> PerformanceOut:
    first = rows[0]
    by_horizon = {row["horizon"]: row for row in rows}
    return PerformanceOut.model_validate(
        {
            **first,
            "name": first["instrument_name"],
            "measured_in": _measured_in(first["unit"]),
            "converted": _converted(first, currency),
            "returns": [
                HorizonOut(
                    horizon=horizon,
                    since=by_horizon[horizon]["since"],
                    past_close=by_horizon[horizon]["past_close"],
                    value=by_horizon[horizon]["performance"],
                )
                for horizon in HORIZONS
                if horizon in by_horizon
            ],
        }
    )


def _measured_in(unit: str) -> str:
    return "points" if unit == "percent" else "return"


async def daily(
    read: SqlReadRepository,
    analytics: SqlAnalyticsRepository,
    *,
    instrument_id: uuid.UUID,
    observation_type: ObservationType | None,
    provider: str | None,
    currency: str | None,
    start: datetime,
    end: datetime,
    limit: int,
    offset: int,
) -> Page[DailyCloseOut]:
    await instrument_row(read, instrument_id)
    await check_provider(read, provider)
    rows = await analytics.daily(
        instrument_id=instrument_id,
        observation_type=observation_type,
        provider=provider,
        currency=currency,
        start=start,
        end=end,
        limit=limit + 1,
        offset=offset,
    )
    return to_page(
        rows,
        limit,
        offset,
        lambda row: DailyCloseOut.model_validate({**row, "converted": _converted(row, currency)}),
    )


async def period_returns(
    read: SqlReadRepository,
    analytics: SqlAnalyticsRepository,
    *,
    instrument_id: uuid.UUID,
    period: str,
    observation_type: ObservationType | None,
    provider: str | None,
    start: datetime,
    end: datetime,
    limit: int,
    offset: int,
) -> Page[PeriodReturnOut]:
    await instrument_row(read, instrument_id)
    await check_provider(read, provider)
    rows = await analytics.period_returns(
        instrument_id=instrument_id,
        period=period,
        observation_type=observation_type,
        provider=provider,
        start=start,
        end=end,
        limit=limit + 1,
        offset=offset,
    )
    return to_page(rows, limit, offset, PeriodReturnOut.model_validate)


async def relative_performance(
    read: SqlReadRepository,
    analytics: SqlAnalyticsRepository,
    *,
    instrument_ids: Collection[uuid.UUID] | None,
    list_name: str | None,
    provider: str | None,
    start: datetime,
    end: datetime,
    limit: int,
    offset: int,
) -> Page[RelativePointOut]:
    await check_provider(read, provider)
    ids = list(dict.fromkeys(instrument_ids or []))
    if list_name is not None:
        members = await analytics.list_instrument_ids(list_name)
        if members is None:
            raise _not_found("list_not_found", f"No watchlist or group named {list_name!r}.")
        ids += [member for member in members if member not in ids]
    if not ids:
        raise AppError(
            "nothing_to_compare",
            "Give at least one instrument_id, or a list.",
            HTTPStatus.UNPROCESSABLE_ENTITY,
        )
    if len(ids) > MAX_COMPARED:
        raise AppError(
            "too_many_instruments",
            f"Compare at most {MAX_COMPARED} instruments at a time.",
            HTTPStatus.UNPROCESSABLE_ENTITY,
            {"instruments": len(ids)},
        )
    rows = await analytics.relative_performance(
        instrument_ids=ids,
        provider=provider,
        start=start,
        end=end,
        limit=limit + 1,
        offset=offset,
    )
    return to_page(rows, limit, offset, RelativePointOut.model_validate)


# --- Spreads and yield curves -----------------------------------------------------------------


async def spreads(analytics: SqlAnalyticsRepository, *, limit: int, offset: int) -> Page[SpreadOut]:
    rows = await analytics.spreads(limit=limit + 1, offset=offset)

    def convert(row: Row) -> SpreadOut:
        return SpreadOut.model_validate(
            {
                **row,
                "long": SpreadLegOut(symbol=row["long_symbol"], provider=row["long_provider"]),
                "short": SpreadLegOut(symbol=row["short_symbol"], provider=row["short_provider"]),
            }
        )

    return to_page(rows, limit, offset, convert)


async def spread_history(
    analytics: SqlAnalyticsRepository,
    *,
    name: str,
    start: datetime,
    end: datetime,
    limit: int,
    offset: int,
) -> Page[SpreadValueOut]:
    if not await analytics.spread_exists(name):
        raise _not_found("spread_not_found", f"No spread named {name!r}.")
    rows = await analytics.spread_history(
        name=name, start=start, end=end, limit=limit + 1, offset=offset
    )
    return to_page(rows, limit, offset, SpreadValueOut.model_validate)


async def yield_curves(
    analytics: SqlAnalyticsRepository, *, curve: str | None, limit: int, offset: int
) -> Page[YieldCurvePointOut]:
    rows = await analytics.yield_curves(curve=curve, limit=limit + 1, offset=offset)
    return to_page(rows, limit, offset, YieldCurvePointOut.model_validate)


# --- Watchlists and groups ------------------------------------------------------------------


async def watchlists(
    analytics: SqlAnalyticsRepository, *, limit: int, offset: int
) -> Page[WatchlistOut]:
    rows = await analytics.lists(kind="watchlist", name=None, limit=limit + 1, offset=offset)
    members = await _members(analytics, rows[:limit])
    return to_page(
        rows, limit, offset, lambda row: WatchlistOut.model_validate(_list(row, members))
    )


async def watchlist(analytics: SqlAnalyticsRepository, name: str) -> WatchlistOut:
    row = await _one_list(analytics, "watchlist", name)
    return WatchlistOut.model_validate(_list(row, await _members(analytics, [row])))


async def groups(analytics: SqlAnalyticsRepository, *, limit: int, offset: int) -> Page[GroupOut]:
    rows = await analytics.lists(kind="group", name=None, limit=limit + 1, offset=offset)
    members = await _members(analytics, rows[:limit])
    performance = await _group_performance(analytics, rows[:limit])
    return to_page(
        rows,
        limit,
        offset,
        lambda row: GroupOut.model_validate(
            {**_list(row, members), "performance": performance.get(row["name"], [])}
        ),
    )


async def group(analytics: SqlAnalyticsRepository, name: str) -> GroupOut:
    row = await _one_list(analytics, "group", name)
    members = await _members(analytics, [row])
    performance = await _group_performance(analytics, [row])
    return GroupOut.model_validate(
        {**_list(row, members), "performance": performance.get(name, [])}
    )


async def _one_list(analytics: SqlAnalyticsRepository, kind: str, name: str) -> Row:
    rows = await analytics.lists(kind=kind, name=name, limit=1, offset=0)
    if not rows:
        raise _not_found(f"{kind}_not_found", f"No {kind} named {name!r}.")
    return rows[0]


async def _members(
    analytics: SqlAnalyticsRepository, rows: list[Row]
) -> dict[str, list[ListMemberOut]]:
    by_list: dict[str, list[ListMemberOut]] = {row["name"]: [] for row in rows}
    if rows:
        for member in await analytics.list_members(list(by_list)):
            by_list[member["list"]].append(ListMemberOut.model_validate(member))
    return by_list


def _list(row: Row, members: dict[str, list[ListMemberOut]]) -> dict[str, object]:
    return {**row, "members": members.get(row["name"], [])}


async def _group_performance(
    analytics: SqlAnalyticsRepository, rows: list[Row]
) -> dict[str, list[GroupHorizonOut]]:
    by_group: dict[str, dict[str, Row]] = {row["name"]: {} for row in rows}
    if rows:
        for value in await analytics.group_performance(list(by_group)):
            by_group[value["group"]][value["horizon"]] = value
    return {
        name: [
            GroupHorizonOut(
                horizon=horizon,
                value=values[horizon]["performance"],
                members_with_value=values[horizon]["members_with_value"],
                members=values[horizon]["members"],
            )
            for horizon in HORIZONS
            if horizon in values
        ]
        for name, values in by_group.items()
    }


# --- Portfolios -----------------------------------------------------------------------------


def _summary(row: Row) -> dict[str, object]:
    value: Decimal | None = row["value"]
    cost: Decimal | None = row["cost"]
    profit = value - cost if value is not None and cost is not None else None
    percent = value / cost - 1 if profit is not None and cost else None
    return {**row, "profit": profit, "profit_percent": percent}


async def portfolios(
    analytics: SqlAnalyticsRepository, *, limit: int, offset: int
) -> Page[PortfolioSummaryOut]:
    rows = await analytics.portfolios(name=None, limit=limit + 1, offset=offset)
    return to_page(
        rows, limit, offset, lambda row: PortfolioSummaryOut.model_validate(_summary(row))
    )


async def portfolio(analytics: SqlAnalyticsRepository, name: str) -> PortfolioOut:
    rows = await analytics.portfolios(name=name, limit=1, offset=0)
    if not rows:
        raise _not_found("portfolio_not_found", f"No portfolio named {name!r}.")
    holdings = await analytics.portfolio_holdings(name)
    return PortfolioOut.model_validate(
        {
            **_summary(rows[0]),
            "positions": [
                HoldingOut.model_validate({**h, "name": h["instrument_name"]}) for h in holdings
            ],
        }
    )


async def portfolio_history(
    analytics: SqlAnalyticsRepository,
    *,
    name: str,
    start: datetime,
    end: datetime,
    limit: int,
    offset: int,
) -> Page[PortfolioValueOut]:
    if not await analytics.portfolios(name=name, limit=1, offset=0):
        raise _not_found("portfolio_not_found", f"No portfolio named {name!r}.")
    rows = await analytics.portfolio_history(
        name=name, start=start, end=end, limit=limit + 1, offset=offset
    )
    return to_page(rows, limit, offset, PortfolioValueOut.model_validate)
