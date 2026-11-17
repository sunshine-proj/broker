"""Database models. Importing this package registers every table on `Base.metadata`."""

from app.models.base import Base
from app.models.economic import EconomicIndicator, EconomicObservation
from app.models.lists import (
    InstrumentList,
    InstrumentListMember,
    Portfolio,
    PortfolioHolding,
    YieldSpread,
)
from app.models.market_data import OHLCV, MarketObservation
from app.models.operations import (
    CollectionJob,
    CollectorExecution,
    DatabaseBackup,
    ProviderHealth,
)
from app.models.quality import DataQualityIssue
from app.models.reference import Exchange, Instrument, InstrumentProviderMapping, Provider

__all__ = [
    "OHLCV",
    "Base",
    "CollectionJob",
    "CollectorExecution",
    "DataQualityIssue",
    "DatabaseBackup",
    "EconomicIndicator",
    "EconomicObservation",
    "Exchange",
    "Instrument",
    "InstrumentList",
    "InstrumentListMember",
    "InstrumentProviderMapping",
    "MarketObservation",
    "Portfolio",
    "PortfolioHolding",
    "Provider",
    "ProviderHealth",
    "YieldSpread",
]
