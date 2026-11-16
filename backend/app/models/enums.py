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


class CollectorStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class ProviderHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"


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
