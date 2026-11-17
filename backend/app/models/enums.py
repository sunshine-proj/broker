"""Enumerations of the data model (PRD section 7).

Values are stored as text with a CHECK constraint. Adding a value needs a
migration that replaces the constraint.
"""

from enum import StrEnum

import sqlalchemy as sa


class AssetType(StrEnum):
    STOCK = "stock"
    ETF = "etf"
    CRYPTO = "crypto"
    FOREX = "forex"
    GOVERNMENT_BOND_YIELD = "government_bond_yield"
    INTEREST_RATE = "interest_rate"
    COMMODITY = "commodity"
    INDEX = "index"


class ObservationType(StrEnum):
    PRICE = "price"
    NAV = "nav"
    RATE = "rate"
    YIELD = "yield"
    INDEX_LEVEL = "index_level"
    MARKET_CAP = "market_cap"
    VOLUME = "volume"
    CIRCULATING_SUPPLY = "circulating_supply"
    TOTAL_SUPPLY = "total_supply"
    DOMINANCE = "dominance"


class Freshness(StrEnum):
    REAL_TIME = "real_time"
    DELAYED = "delayed"
    END_OF_DAY = "end_of_day"


class QuoteKind(StrEnum):
    TRADED = "traded"
    INDICATIVE = "indicative"
    REFERENCE = "reference"


class QualityStatus(StrEnum):
    OK = "ok"
    SUSPECT = "suspect"


class IndicatorFrequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class CandleInterval(StrEnum):
    MIN_1 = "1min"
    MIN_5 = "5min"
    MIN_15 = "15min"
    MIN_30 = "30min"
    HOUR_1 = "1h"
    HOUR_4 = "4h"
    DAY_1 = "1day"
    WEEK_1 = "1week"
    MONTH_1 = "1month"


class JobKind(StrEnum):
    # Latest values of the provider's instruments.
    QUOTES = "quotes"
    # Closed candles over a recent window.
    CANDLES = "candles"
    # Past values over a recent window.
    HISTORY = "history"
    # Economic indicator values over a recent window.
    INDICATORS = "indicators"
    # Data-quality checks over every provider's data (the only kind without a provider).
    QUALITY = "quality"
    # Re-fetch ranges the quality checks found missing.
    BACKFILL = "backfill"


class IssueKind(StrEnum):
    STALE = "stale"
    GAP = "gap"
    CONFLICT = "conflict"
    SPIKE = "spike"
    DISCREPANCY = "discrepancy"


class IssueStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class IssueResolution(StrEnum):
    # The condition no longer holds (stale, discrepancy).
    CLEARED = "cleared"
    # Backfill stored values in the gap.
    FILLED = "filled"
    # Backfill found nothing at the provider for the gap.
    NO_DATA = "no_data"
    # The provider cannot re-fetch the gap, or failed to repeatedly.
    UNRECOVERABLE = "unrecoverable"
    # An event recorded for information (conflict, spike).
    RECORDED = "recorded"


class ExecutionTrigger(StrEnum):
    SCHEDULED = "scheduled"
    MANUAL = "manual"
    # A one-off rerun after a failed run.
    RETRY = "retry"


class CollectorStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class ProviderHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


class UserRole(StrEnum):
    """What a user may do (PRD FR-017). Both roles read everything."""

    VIEWER = "viewer"
    ADMIN = "admin"


def enum_type(enum_class: type[StrEnum], name: str, length: int = 32) -> sa.Enum:
    """Column type for an enumeration; `name` becomes part of the CHECK constraint name."""
    return sa.Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=length,
        values_callable=lambda members: [member.value for member in members],
    )
