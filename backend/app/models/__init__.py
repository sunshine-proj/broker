"""Database models. Importing this package registers every table on `Base.metadata`."""

from app.models.base import Base
from app.models.economic import EconomicIndicator, EconomicObservation
from app.models.market_data import OHLCV, MarketObservation
from app.models.operations import CollectorExecution, ProviderHealth
from app.models.reference import Exchange, Instrument, InstrumentProviderMapping, Provider

__all__ = [
    "OHLCV",
    "Base",
    "CollectorExecution",
    "EconomicIndicator",
    "EconomicObservation",
    "Exchange",
    "Instrument",
    "InstrumentProviderMapping",
    "MarketObservation",
    "Provider",
    "ProviderHealth",
]
