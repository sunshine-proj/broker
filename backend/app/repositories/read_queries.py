"""Queries behind the read API.

They return plain rows, with provider names and instrument fields joined in.
Every list query takes `limit` and `offset`; callers ask for one row more than
they show to find out whether another page exists.
"""

import uuid
from collections import defaultdict
from collections.abc import Collection
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    OHLCV,
    CollectionJob,
    CollectorExecution,
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
    CollectorStatus,
    IndicatorFrequency,
    ObservationType,
    QuoteKind,
)

Row = dict[str, Any]

_instrument = Instrument.__table__
_exchange = Exchange.__table__
_mapping = InstrumentProviderMapping.__table__
_provider = Provider.__table__
_observation = MarketObservation.__table__
_ohlcv = OHLCV.__table__
_indicator = EconomicIndicator.__table__
_economic = EconomicObservation.__table__
_health = ProviderHealth.__table__
_job = CollectionJob.__table__
_execution = CollectorExecution.__table__


class SqlReadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _rows(self, statement: sa.Select) -> list[Row]:
        return [dict(row) for row in (await self._session.execute(statement)).mappings()]

    # --- Instruments ---------------------------------------------------------------------

    def _instrument_select(self) -> sa.Select:
        return sa.select(
            _instrument.c.id,
            _instrument.c.symbol,
            _instrument.c.name,
            _instrument.c.asset_type,
            _exchange.c.code.label("exchange"),
            _instrument.c.currency,
            _instrument.c.enabled,
            _instrument.c.metadata,
        ).select_from(_instrument.outerjoin(_exchange, _exchange.c.id == _instrument.c.exchange_id))

    async def list_instruments(
        self,
        *,
        asset_type: AssetType | None,
        enabled: bool | None,
        provider: str | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> list[Row]:
        statement = self._instrument_select()
        if asset_type is not None:
            statement = statement.where(_instrument.c.asset_type == asset_type)
        if enabled is not None:
            statement = statement.where(_instrument.c.enabled == enabled)
        if provider is not None:
            statement = statement.where(
                sa.exists().where(
                    _mapping.c.instrument_id == _instrument.c.id,
                    _mapping.c.provider_id == _provider.c.id,
                    _provider.c.name == provider,
                )
            )
        if search:
            pattern = f"%{_escape_like(search)}%"
            statement = statement.where(
                sa.or_(
                    _instrument.c.symbol.ilike(pattern, escape="\\"),
                    _instrument.c.name.ilike(pattern, escape="\\"),
                )
            )
        statement = (
            statement.order_by(_instrument.c.asset_type, _instrument.c.symbol, _instrument.c.id)
            .limit(limit)
            .offset(offset)
        )
        rows = await self._rows(statement)
        await self._attach_mappings(rows)
        return rows

    async def get_instrument(self, instrument_id: uuid.UUID) -> Row | None:
        rows = await self._rows(self._instrument_select().where(_instrument.c.id == instrument_id))
        await self._attach_mappings(rows)
        return rows[0] if rows else None

    async def _attach_mappings(self, rows: list[Row]) -> None:
        ids = [row["id"] for row in rows]
        by_instrument: dict[uuid.UUID, list[Row]] = defaultdict(list)
        if ids:
            statement = (
                sa.select(
                    _mapping.c.instrument_id,
                    _provider.c.name.label("provider"),
                    _mapping.c.provider_symbol,
                    _mapping.c.enabled,
                )
                .join(_provider, _provider.c.id == _mapping.c.provider_id)
                .where(_mapping.c.instrument_id.in_(ids))
                .order_by(_provider.c.name)
            )
            for mapping in await self._rows(statement):
                by_instrument[mapping["instrument_id"]].append(mapping)
        for row in rows:
            row["providers"] = by_instrument[row["id"]]

    async def provider_exists(self, name: str) -> bool:
        statement = sa.select(sa.exists().where(_provider.c.name == name))
        return bool(await self._session.scalar(statement))

    # --- Market observations ---------------------------------------------------------------

    async def latest_observations(
        self,
        *,
        observation_types: Collection[ObservationType],
        asset_types: Collection[AssetType] | None = None,
        instrument_ids: Collection[uuid.UUID] | None = None,
        symbols: Collection[str] | None = None,
        provider: str | None = None,
        quote_kind: QuoteKind | None = None,
        country: str | None = None,
        base_currency: str | None = None,
        quote_currency: str | None = None,
        limit: int,
        offset: int,
    ) -> list[Row]:
        """The most recent observation of each instrument, provider and observation type.

        Each series is looked up on its own, newest first through the primary
        key, so the cost does not grow with the length of the history.
        """
        types = sa.values(sa.column("observation_type", sa.String(32)), name="wanted").data(
            [(t.value,) for t in observation_types]
        )
        latest = (
            sa.select(_observation)
            .where(
                _observation.c.instrument_id == _mapping.c.instrument_id,
                _observation.c.provider_id == _mapping.c.provider_id,
                _observation.c.observation_type == types.c.observation_type,
            )
            .order_by(_observation.c.observed_at.desc())
            .limit(1)
            .lateral("latest")
        )
        statement = (
            sa.select(
                latest,
                _instrument.c.symbol,
                _instrument.c.asset_type,
                _instrument.c.metadata.label("instrument_metadata"),
                _provider.c.name.label("provider"),
            )
            .select_from(
                _mapping.join(_instrument, _instrument.c.id == _mapping.c.instrument_id)
                .join(_provider, _provider.c.id == _mapping.c.provider_id)
                .join(types, sa.true())
                .join(latest, sa.true())
            )
            .order_by(
                _instrument.c.asset_type,
                _instrument.c.symbol,
                _provider.c.name,
                latest.c.observation_type,
            )
            .limit(limit)
            .offset(offset)
        )
        if asset_types is not None:
            statement = statement.where(_instrument.c.asset_type.in_(list(asset_types)))
        if instrument_ids is not None:
            statement = statement.where(_instrument.c.id.in_(list(instrument_ids)))
        if symbols is not None:
            statement = statement.where(_instrument.c.symbol.in_(list(symbols)))
        if provider is not None:
            statement = statement.where(_provider.c.name == provider)
        if quote_kind is not None:
            statement = statement.where(latest.c.quote_kind == quote_kind)
        if country is not None:
            statement = statement.where(_instrument.c.metadata["country"].astext == country)
        # Pairs are written BASE/QUOTE.
        if base_currency is not None:
            pattern = f"{_escape_like(base_currency)}/%"
            statement = statement.where(_instrument.c.symbol.like(pattern, escape="\\"))
        if quote_currency is not None:
            pattern = f"%/{_escape_like(quote_currency)}"
            statement = statement.where(_instrument.c.symbol.like(pattern, escape="\\"))
        return await self._rows(statement)

    async def observation_history(
        self,
        *,
        instrument_id: uuid.UUID,
        observation_type: ObservationType,
        start: datetime,
        end: datetime,
        provider: str | None,
        limit: int,
        offset: int,
    ) -> list[Row]:
        statement = (
            sa.select(
                _observation,
                _instrument.c.symbol,
                _instrument.c.asset_type,
                _provider.c.name.label("provider"),
            )
            .join(_instrument, _instrument.c.id == _observation.c.instrument_id)
            .join(_provider, _provider.c.id == _observation.c.provider_id)
            .where(
                _observation.c.instrument_id == instrument_id,
                _observation.c.observation_type == observation_type,
                _observation.c.observed_at >= start,
                _observation.c.observed_at < end,
            )
            .order_by(_observation.c.observed_at, _provider.c.name)
            .limit(limit)
            .offset(offset)
        )
        if provider is not None:
            statement = statement.where(_provider.c.name == provider)
        return await self._rows(statement)

    async def candles(
        self,
        *,
        instrument_id: uuid.UUID,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
        provider: str | None,
        limit: int,
        offset: int,
    ) -> list[Row]:
        statement = (
            sa.select(_ohlcv, _provider.c.name.label("provider"))
            .join(_provider, _provider.c.id == _ohlcv.c.provider_id)
            .where(
                _ohlcv.c.instrument_id == instrument_id,
                _ohlcv.c.interval == interval,
                _ohlcv.c.timestamp >= start,
                _ohlcv.c.timestamp < end,
            )
            .order_by(_ohlcv.c.timestamp, _provider.c.name)
            .limit(limit)
            .offset(offset)
        )
        if provider is not None:
            statement = statement.where(_provider.c.name == provider)
        return await self._rows(statement)

    # --- Economic indicators -----------------------------------------------------------------

    async def indicators(
        self,
        *,
        country: str | None,
        provider: str | None,
        frequency: IndicatorFrequency | None,
        enabled: bool | None,
        limit: int,
        offset: int,
    ) -> list[Row]:
        """Indicators with the current value of their most recent period."""
        latest = (
            sa.select(
                _economic.c.observed_at,
                _economic.c.ingested_at,
                _economic.c.value,
                _economic.c.metadata,
            )
            .where(_economic.c.indicator_id == _indicator.c.id)
            .order_by(_economic.c.observed_at.desc(), _economic.c.ingested_at.desc())
            .limit(1)
            .lateral("latest")
        )
        statement = (
            sa.select(
                _indicator,
                _provider.c.name.label("provider"),
                latest.c.observed_at.label("latest_observed_at"),
                latest.c.ingested_at.label("latest_ingested_at"),
                latest.c.value.label("latest_value"),
                latest.c.metadata.label("latest_metadata"),
            )
            .select_from(
                _indicator.join(_provider, _provider.c.id == _indicator.c.provider_id).outerjoin(
                    latest, sa.true()
                )
            )
            .order_by(_indicator.c.code)
            .limit(limit)
            .offset(offset)
        )
        if country is not None:
            statement = statement.where(_indicator.c.country == country)
        if provider is not None:
            statement = statement.where(_provider.c.name == provider)
        if frequency is not None:
            statement = statement.where(_indicator.c.frequency == frequency)
        if enabled is not None:
            statement = statement.where(_indicator.c.enabled == enabled)
        return await self._rows(statement)

    async def indicator_observations(
        self,
        *,
        indicator_id: uuid.UUID,
        start: datetime,
        end: datetime,
        include_revisions: bool,
        limit: int,
        offset: int,
    ) -> list[Row]:
        statement = sa.select(
            _economic.c.observed_at,
            _economic.c.ingested_at,
            _economic.c.value,
            _economic.c.metadata,
        ).where(
            _economic.c.indicator_id == indicator_id,
            _economic.c.observed_at >= start,
            _economic.c.observed_at < end,
        )
        if include_revisions:
            statement = statement.order_by(_economic.c.observed_at, _economic.c.ingested_at)
        else:
            statement = statement.ext(distinct_on(_economic.c.observed_at)).order_by(
                _economic.c.observed_at, _economic.c.ingested_at.desc()
            )
        return await self._rows(statement.limit(limit).offset(offset))

    async def indicator_id(self, code: str) -> uuid.UUID | None:
        return await self._session.scalar(
            sa.select(_indicator.c.id).where(_indicator.c.code == code)
        )

    # --- Providers and collection --------------------------------------------------------------

    async def providers(self, *, limit: int, offset: int) -> list[Row]:
        statement = (
            sa.select(
                _provider.c.name,
                _provider.c.base_url,
                _provider.c.enabled,
                _provider.c.configuration,
                _health.c.status.label("health"),
                _health.c.last_success_at,
                _health.c.last_error_at,
                _health.c.last_error,
                sa.func.coalesce(_health.c.consecutive_failures, 0).label("consecutive_failures"),
            )
            .select_from(_provider.outerjoin(_health, _health.c.provider_id == _provider.c.id))
            .order_by(_provider.c.name)
            .limit(limit)
            .offset(offset)
        )
        return await self._rows(statement)

    async def jobs(
        self, *, provider: str | None, enabled: bool | None, limit: int, offset: int
    ) -> list[Row]:
        statement = (
            sa.select(_job, _provider.c.name.label("provider"))
            .join(_provider, _provider.c.id == _job.c.provider_id)
            .order_by(_job.c.name)
            .limit(limit)
            .offset(offset)
        )
        if provider is not None:
            statement = statement.where(_provider.c.name == provider)
        if enabled is not None:
            statement = statement.where(_job.c.enabled == enabled)
        return await self._rows(statement)

    async def executions(
        self,
        *,
        job: str | None,
        status: CollectorStatus | None,
        limit: int,
        offset: int,
    ) -> list[Row]:
        statement = (
            sa.select(_execution, _provider.c.name.label("provider"))
            .select_from(
                _execution.outerjoin(_provider, _provider.c.id == _execution.c.provider_id)
            )
            .order_by(_execution.c.started_at.desc(), _execution.c.id)
            .limit(limit)
            .offset(offset)
        )
        if job is not None:
            statement = statement.where(_execution.c.job_name == job)
        if status is not None:
            statement = statement.where(_execution.c.status == status)
        return await self._rows(statement)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
