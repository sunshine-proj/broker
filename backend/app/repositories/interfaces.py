"""Repository interfaces.

Services depend on these protocols, not on the SQLAlchemy implementations, so
they can be tested with in-memory fakes. Repositories never commit: the caller
owns the transaction.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Protocol

from app.models import (
    OHLCV,
    AppUser,
    EconomicIndicator,
    EconomicObservation,
    Exchange,
    Instrument,
    InstrumentProviderMapping,
    MarketObservation,
    Provider,
    ProviderHealth,
)
from app.models.enums import (
    AssetType,
    CandleInterval,
    IndicatorFrequency,
    JobKind,
    ObservationType,
    UserRole,
)
from app.schemas.market_data import EconomicObservationIn, MarketObservationIn, OHLCVIn


@dataclass(frozen=True)
class Conflict:
    """A stored time-series row that a provider re-sent with different values."""

    # The row's key columns, by name.
    key: dict[str, Any]
    stored: dict[str, Any]
    incoming: dict[str, Any]


@dataclass(frozen=True)
class WriteResult:
    """Outcome of writing immutable time-series rows."""

    inserted: int = 0
    # Already stored with the same values.
    unchanged: int = 0
    # Already stored with different values; the stored row was kept.
    conflicting: int = 0
    conflicts: tuple[Conflict, ...] = ()


@dataclass(frozen=True)
class RevisionWriteResult:
    """Outcome of writing revisable economic observations."""

    # First value for a period.
    inserted: int = 0
    # New value for a period that already had one; both are kept.
    revised: int = 0
    unchanged: int = 0


class ProviderRepository(Protocol):
    async def upsert(
        self, *, name: str, base_url: str, enabled: bool, configuration: dict[str, Any]
    ) -> uuid.UUID: ...

    async def get_by_name(self, name: str) -> Provider | None: ...

    async def list(self, *, enabled: bool | None = None) -> Sequence[Provider]: ...


class ProviderHealthRepository(Protocol):
    async def record_success(self, provider_id: uuid.UUID, at: datetime) -> None: ...

    async def record_failure(
        self, provider_id: uuid.UUID, at: datetime, error: str, *, down_after: int
    ) -> None: ...

    async def get(self, provider_id: uuid.UUID) -> ProviderHealth | None: ...

    async def list(self) -> Sequence[ProviderHealth]: ...


class InstrumentRepository(Protocol):
    async def upsert_exchange(
        self,
        *,
        code: str,
        name: str,
        country: str | None,
        timezone: str,
        trading_open: time | None = None,
        trading_close: time | None = None,
    ) -> uuid.UUID: ...

    async def get_exchange_by_code(self, code: str) -> Exchange | None: ...

    async def upsert(
        self,
        *,
        symbol: str,
        name: str,
        asset_type: AssetType,
        exchange_id: uuid.UUID | None,
        currency: str | None,
        enabled: bool,
        metadata: dict[str, Any],
    ) -> uuid.UUID: ...

    async def get(self, instrument_id: uuid.UUID) -> Instrument | None: ...

    async def get_by_symbol(
        self, asset_type: AssetType, symbol: str, exchange_id: uuid.UUID | None = None
    ) -> Instrument | None: ...

    async def list(
        self,
        *,
        asset_type: AssetType | None = None,
        enabled: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Instrument]: ...

    async def upsert_mapping(
        self,
        *,
        instrument_id: uuid.UUID,
        provider_id: uuid.UUID,
        provider_symbol: str,
        enabled: bool = True,
    ) -> None: ...

    async def list_mappings(
        self, provider_id: uuid.UUID, *, enabled_only: bool = True
    ) -> Sequence[InstrumentProviderMapping]: ...

    async def get_instruments_by_provider_symbol(
        self, provider_id: uuid.UUID
    ) -> dict[str, Instrument]: ...


class MarketDataRepository(Protocol):
    async def add_observations(
        self, observations: Sequence[MarketObservationIn]
    ) -> WriteResult: ...

    async def mark_suspect(self, keys: Sequence[Mapping[str, Any]]) -> int: ...

    async def get_observations(
        self,
        instrument_id: uuid.UUID,
        observation_type: ObservationType,
        *,
        start: datetime,
        end: datetime,
        provider_id: uuid.UUID | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Sequence[MarketObservation]: ...

    async def get_latest_observations(
        self, instrument_ids: Sequence[uuid.UUID], observation_type: ObservationType
    ) -> Sequence[MarketObservation]: ...

    async def add_candles(self, candles: Sequence[OHLCVIn]) -> WriteResult: ...

    async def get_candles(
        self,
        instrument_id: uuid.UUID,
        interval: CandleInterval,
        *,
        start: datetime,
        end: datetime,
        provider_id: uuid.UUID | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> Sequence[OHLCV]: ...


class EconomicDataRepository(Protocol):
    async def upsert_indicator(
        self,
        *,
        code: str,
        name: str,
        country: str | None,
        unit: str,
        frequency: IndicatorFrequency,
        provider_id: uuid.UUID,
        provider_series_code: str,
        enabled: bool,
        metadata: dict[str, Any],
    ) -> uuid.UUID: ...

    async def get_indicator_by_code(self, code: str) -> EconomicIndicator | None: ...

    async def list_indicators(
        self,
        *,
        provider_id: uuid.UUID | None = None,
        enabled: bool | None = None,
        limit: int | None = 100,
        offset: int = 0,
    ) -> Sequence[EconomicIndicator]: ...

    async def add_observations(
        self,
        observations: Sequence[EconomicObservationIn],
        *,
        ingested_at: datetime | None = None,
    ) -> RevisionWriteResult: ...

    async def get_observations(
        self,
        indicator_id: uuid.UUID,
        *,
        start: datetime,
        end: datetime,
        include_revisions: bool = False,
        limit: int = 1000,
        offset: int = 0,
    ) -> Sequence[EconomicObservation]: ...


class CollectionJobRepository(Protocol):
    async def upsert(
        self,
        *,
        name: str,
        provider_id: uuid.UUID | None,
        kind: JobKind,
        interval_minutes: int | None,
        daily_at: time | None,
        market_hours_only: bool,
        retry_minutes: int | None,
        settings: dict[str, Any],
        enabled: bool,
    ) -> uuid.UUID: ...

    async def disable_others(self, names: Sequence[str]) -> int: ...


class ListRepository(Protocol):
    """Watchlists, groups and yield spreads (PRD FR-014, FR-015)."""

    async def upsert_list(
        self,
        *,
        kind: str,
        name: str,
        title: str,
        description: str | None,
        enabled: bool,
        instrument_ids: Sequence[uuid.UUID],
    ) -> uuid.UUID: ...

    async def upsert_spread(
        self,
        *,
        name: str,
        title: str,
        long: tuple[uuid.UUID, uuid.UUID],
        short: tuple[uuid.UUID, uuid.UUID],
        enabled: bool,
    ) -> uuid.UUID: ...

    async def disable_others(
        self, list_names: Sequence[str], spread_names: Sequence[str]
    ) -> int: ...


@dataclass(frozen=True)
class HoldingTarget:
    """An instrument and a provider that prices it: what a holding can name."""

    symbol: str
    provider: str
    instrument_id: uuid.UUID
    provider_id: uuid.UUID
    asset_type: AssetType


@dataclass(frozen=True)
class HoldingRecord:
    instrument_id: uuid.UUID
    provider_id: uuid.UUID
    quantity: Decimal
    cost_basis: Decimal
    cost_currency: str
    acquired_on: date | None
    note: str | None


@dataclass(frozen=True)
class PortfolioRecord:
    name: str
    base_currency: str
    holdings: Sequence[HoldingRecord]


class PortfolioRepository(Protocol):
    """Portfolios from the private portfolio file (PRD FR-016)."""

    async def holding_targets(self) -> Sequence[HoldingTarget]: ...

    async def replace_all(self, portfolios: Sequence[PortfolioRecord]) -> None: ...


class AuthRepository(Protocol):
    """User accounts, sessions and API tokens (PRD FR-017). Tokens are passed as hashes."""

    async def get_user(self, username: str) -> AppUser | None: ...

    async def list_users(self) -> Sequence[AppUser]: ...

    async def create_user(
        self, *, username: str, password_hash: str, role: UserRole
    ) -> uuid.UUID: ...

    async def update_user(self, user_id: uuid.UUID, **values: Any) -> None: ...

    async def add_session(
        self, *, user_id: uuid.UUID, token_hash: str, expires_at: datetime
    ) -> None: ...

    async def session_user(self, token_hash: str, now: datetime) -> AppUser | None: ...

    async def revoke_session(self, token_hash: str, now: datetime) -> None: ...

    async def add_token(
        self, *, user_id: uuid.UUID, name: str, token_hash: str, expires_at: datetime | None
    ) -> uuid.UUID: ...

    async def token_user(self, token_hash: str, now: datetime) -> AppUser | None: ...

    async def list_tokens(self) -> Sequence[Mapping[str, Any]]: ...

    async def revoke_token(self, user_id: uuid.UUID, name: str, now: datetime) -> bool: ...

    async def revoke_credentials(
        self, user_id: uuid.UUID, now: datetime, *, tokens: bool = True
    ) -> None: ...
