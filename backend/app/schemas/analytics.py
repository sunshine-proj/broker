"""Response models of the analytics, list and portfolio endpoints (PRD FR-014 to FR-016).

Decimals are JSON strings and timestamps UTC with a `Z`, as in the rest of
the API. Daily values are dated by the start of their UTC day. Returns are
fractions (0.05 is 5 %); for values in percent (yields, rates) changes are
given in percentage points instead, and `measured_in` says which.
"""

import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.models.enums import AssetType, ObservationType
from app.schemas.api import DecimalString, UtcTimestamp, _Response

MeasuredIn = Literal["return", "points"]
Horizon = Literal["1w", "1m", "3m", "ytd", "1y"]


# --- Performance ----------------------------------------------------------------------------


class HorizonOut(BaseModel):
    horizon: Horizon
    # The close compared with: the last at or before the horizon's date.
    since: UtcTimestamp | None
    past_close: DecimalString | None
    value: DecimalString | None


class ConvertedOut(BaseModel):
    currency: str
    # Empty when no exchange rate is known for the day; never estimated.
    close: DecimalString | None


class PerformanceOut(_Response):
    """One series' latest daily close with its returns, averages and volatility."""

    instrument_id: uuid.UUID
    symbol: str
    name: str
    asset_type: AssetType
    provider: str
    observation_type: ObservationType
    unit: str
    currency: str | None
    measured_in: MeasuredIn
    day: UtcTimestamp
    close: DecimalString
    converted: ConvertedOut | None
    daily_change: DecimalString | None
    daily_return: DecimalString | None
    returns: list[HorizonOut]
    sma_20: DecimalString | None
    sma_50: DecimalString | None
    sma_200: DecimalString | None
    # Annualised standard deviation of daily log returns (of daily changes in
    # points, for values in percent).
    volatility_20d: DecimalString | None
    volatility_60d: DecimalString | None


class DailyCloseOut(_Response):
    instrument_id: uuid.UUID
    symbol: str
    provider: str
    observation_type: ObservationType
    unit: str
    currency: str | None
    day: UtcTimestamp
    close: DecimalString
    # `candle` (the provider's daily candle) or `observations` (the day's last value).
    source: str
    converted: ConvertedOut | None
    daily_change: DecimalString | None
    daily_return: DecimalString | None
    sma_20: DecimalString | None
    sma_50: DecimalString | None
    sma_200: DecimalString | None
    volatility_20d: DecimalString | None
    volatility_60d: DecimalString | None


class PeriodReturnOut(_Response):
    instrument_id: uuid.UUID
    symbol: str
    provider: str
    observation_type: ObservationType
    unit: str
    period: Literal["week", "month"]
    period_start: UtcTimestamp
    last_day: UtcTimestamp
    close: DecimalString
    previous_close: DecimalString | None
    change: DecimalString | None
    # Empty for values in percent, and for the first period.
    return_: DecimalString | None = Field(validation_alias="return", serialization_alias="return")


class RelativePointOut(_Response):
    instrument_id: uuid.UUID
    symbol: str
    provider: str
    day: UtcTimestamp
    close: DecimalString
    # The series' first close in the range.
    base_close: DecimalString
    # 100 at the first close; empty for values in percent.
    rebased: DecimalString | None


# --- Spreads and yield curves -----------------------------------------------------------------


class SpreadLegOut(BaseModel):
    symbol: str
    provider: str


class SpreadOut(_Response):
    name: str
    title: str
    long: SpreadLegOut
    short: SpreadLegOut
    # The newest day both legs have a value, if any.
    day: UtcTimestamp | None
    spread: DecimalString | None
    spread_bp: DecimalString | None


class SpreadValueOut(_Response):
    day: UtcTimestamp
    long_value: DecimalString
    short_value: DecimalString
    # Long minus short, in percentage points and in basis points.
    spread: DecimalString
    spread_bp: DecimalString


class YieldCurvePointOut(_Response):
    curve: str
    maturity: str
    maturity_months: int
    symbol: str
    provider: str
    day: UtcTimestamp
    latest: DecimalString
    week_ago: DecimalString | None
    month_ago: DecimalString | None
    year_ago: DecimalString | None


# --- Watchlists and groups ------------------------------------------------------------------


class ListMemberOut(_Response):
    position: int
    instrument_id: uuid.UUID
    symbol: str
    name: str
    asset_type: AssetType


class WatchlistOut(_Response):
    name: str
    title: str
    description: str | None
    members: list[ListMemberOut]


class GroupHorizonOut(BaseModel):
    horizon: Horizon
    # The members' average performance, each member weighted equally.
    value: DecimalString | None
    members_with_value: int
    members: int


class GroupOut(WatchlistOut):
    performance: list[GroupHorizonOut]


# --- Portfolios -----------------------------------------------------------------------------


class PortfolioSummaryOut(_Response):
    name: str
    base_currency: str
    loaded_at: UtcTimestamp
    holdings: int
    # Holdings with a current price and exchange rate; totals cover only these.
    valued_holdings: int
    value: DecimalString | None
    cost: DecimalString | None
    profit: DecimalString | None
    profit_percent: DecimalString | None


class HoldingOut(_Response):
    symbol: str
    name: str
    asset_type: AssetType
    provider: str
    quantity: DecimalString
    cost_basis: DecimalString
    cost_currency: str
    acquired_on: date | None
    valued_since: UtcTimestamp | None
    note: str | None
    price_day: UtcTimestamp | None
    price: DecimalString | None
    price_currency: str | None
    # In the price currency, and in the portfolio's base currency.
    value_local: DecimalString | None
    value: DecimalString | None
    cost: DecimalString | None
    profit: DecimalString | None
    profit_percent: DecimalString | None
    weight: DecimalString | None


class PortfolioOut(PortfolioSummaryOut):
    positions: list[HoldingOut]


class PortfolioValueOut(_Response):
    day: UtcTimestamp
    value: DecimalString | None
    cost: DecimalString | None
    holdings: int
    valued_holdings: int
