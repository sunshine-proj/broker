"""Queries behind the analytics, list and portfolio endpoints (PRD FR-014 to FR-016).

The calculations are database views and functions (migration 0009), shared
with Grafana; these queries only filter and order them. List queries take
`limit` and `offset` like the other read queries.
"""

import uuid
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AssetType, ObservationType

Row = dict[str, Any]

_UUIDS = ARRAY(sa.Uuid)
_TEXTS = ARRAY(sa.Text)


class _Query:
    """A statement built from optional conditions, with bound parameters."""

    def __init__(self) -> None:
        self.conditions: list[str] = []
        self.params: dict[str, Any] = {}
        self.types: list[sa.BindParameter[Any]] = []

    def where(self, condition: str, **params: Any) -> None:
        self.conditions.append(condition)
        self.params.update(params)

    def array(self, name: str, values: Collection[Any], type_: Any) -> None:
        self.params[name] = list(values)
        self.types.append(sa.bindparam(name, type_=type_))

    def sql(self) -> str:
        return " AND ".join(self.conditions) if self.conditions else "true"


class SqlAnalyticsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _rows(self, sql: str, query: _Query | None = None, **params: Any) -> list[Row]:
        statement = sa.text(sql)
        if query is not None:
            statement = statement.bindparams(*query.types)
            params = {**query.params, **params}
        result = await self._session.execute(statement, params)
        return [dict(row) for row in result.mappings()]

    # --- Performance --------------------------------------------------------------------

    async def performance(
        self,
        *,
        symbols: Collection[str] | None,
        asset_types: Collection[AssetType] | None,
        provider: str | None,
        list_name: str | None,
        currency: str | None,
    ) -> list[Row]:
        """Rows of v_performance, one per series and horizon, in series order.

        With `list_name`, only that list's members, in list order. With
        `currency`, each latest close is also converted into it.
        """
        query = _Query()
        if symbols is not None:
            query.where("f.symbol = ANY (:symbols)")
            query.array("symbols", symbols, _TEXTS)
        if asset_types is not None:
            query.where("f.asset_type = ANY (:asset_types)")
            query.array("asset_types", [t.value for t in asset_types], _TEXTS)
        if provider is not None:
            query.where("f.provider = :provider", provider=provider)
        join = ""
        order = "f.asset_type, f.symbol"
        if list_name is not None:
            join = "JOIN v_instrument_list m ON m.instrument_id = f.instrument_id"
            query.where("m.list = :list_name", list_name=list_name)
            order = "m.position"
        converted = "NULL::numeric"
        if currency is not None:
            converted = "f.close * fx_rate(f.currency, :currency, f.day)"
            query.params["currency"] = currency
        return await self._rows(
            f"""
            SELECT f.*, {converted} AS converted_close
              FROM v_performance f {join}
             WHERE {query.sql()}
             ORDER BY {order}, f.provider, f.observation_type, f.horizon
            """,
            query,
        )

    async def daily(
        self,
        *,
        instrument_id: uuid.UUID,
        observation_type: ObservationType | None,
        provider: str | None,
        currency: str | None,
        start: datetime,
        end: datetime,
        limit: int,
        offset: int,
    ) -> list[Row]:
        """Daily closes and their metrics, oldest first; the main type by default."""
        query = _Query()
        query.where("d.instrument_id = :instrument_id", instrument_id=instrument_id)
        query.where("d.day >= :start AND d.day < :end", start=start, end=end)
        if observation_type is None:
            query.where("d.observation_type = primary_observation_type(d.asset_type)")
        else:
            query.where("d.observation_type = :type", type=observation_type.value)
        if provider is not None:
            query.where("d.provider = :provider", provider=provider)
        converted = "NULL::numeric"
        if currency is not None:
            converted = "d.close * fx_rate(d.currency, :currency, d.day)"
            query.params["currency"] = currency
        return await self._rows(
            f"""
            SELECT d.*, {converted} AS converted_close
              FROM v_daily_metrics d
             WHERE {query.sql()}
             ORDER BY d.day, d.provider, d.observation_type
             LIMIT :limit OFFSET :offset
            """,
            query,
            limit=limit,
            offset=offset,
        )

    async def period_returns(
        self,
        *,
        instrument_id: uuid.UUID,
        period: str,
        observation_type: ObservationType | None,
        provider: str | None,
        start: datetime,
        end: datetime,
        limit: int,
        offset: int,
    ) -> list[Row]:
        query = _Query()
        query.where("r.instrument_id = :instrument_id", instrument_id=instrument_id)
        query.where("r.period = :period", period=period)
        query.where("r.period_start >= :start AND r.period_start < :end", start=start, end=end)
        if observation_type is None:
            query.where("r.observation_type = primary_observation_type(r.asset_type)")
        else:
            query.where("r.observation_type = :type", type=observation_type.value)
        if provider is not None:
            query.where("r.provider = :provider", provider=provider)
        return await self._rows(
            f"""
            SELECT r.* FROM v_period_return r
             WHERE {query.sql()}
             ORDER BY r.period_start, r.provider, r.observation_type
             LIMIT :limit OFFSET :offset
            """,
            query,
            limit=limit,
            offset=offset,
        )

    async def relative_performance(
        self,
        *,
        instrument_ids: Sequence[uuid.UUID],
        provider: str | None,
        start: datetime,
        end: datetime,
        limit: int,
        offset: int,
    ) -> list[Row]:
        query = _Query()
        query.array("ids", instrument_ids, _UUIDS)
        query.where("r.observation_type = primary_observation_type(i.asset_type)")
        if provider is not None:
            query.where("r.provider = :provider", provider=provider)
        return await self._rows(
            f"""
            SELECT r.* FROM relative_performance(:ids, :start, :end) r
              JOIN instrument i ON i.id = r.instrument_id
             WHERE {query.sql()}
             ORDER BY r.day, r.symbol, r.provider
             LIMIT :limit OFFSET :offset
            """,
            query,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )

    # --- Spreads and curves -----------------------------------------------------------

    async def spreads(self, *, limit: int, offset: int) -> list[Row]:
        """Enabled spreads with their legs and newest value, if any."""
        return await self._rows(
            """
            SELECT s.name, s.title,
                   li.symbol AS long_symbol, lp.name AS long_provider,
                   si.symbol AS short_symbol, sp.name AS short_provider,
                   v.day, v.long_value, v.short_value, v.spread, v.spread_bp
              FROM yield_spread s
              JOIN instrument li ON li.id = s.long_instrument_id
              JOIN provider lp ON lp.id = s.long_provider_id
              JOIN instrument si ON si.id = s.short_instrument_id
              JOIN provider sp ON sp.id = s.short_provider_id
              LEFT JOIN LATERAL (
                   SELECT * FROM v_yield_spread x WHERE x.name = s.name
                    ORDER BY x.day DESC LIMIT 1) v ON true
             WHERE s.enabled
             ORDER BY s.name
             LIMIT :limit OFFSET :offset
            """,
            limit=limit,
            offset=offset,
        )

    async def spread_exists(self, name: str) -> bool:
        rows = await self._rows(
            "SELECT 1 FROM yield_spread WHERE name = :name AND enabled", name=name
        )
        return bool(rows)

    async def spread_history(
        self, *, name: str, start: datetime, end: datetime, limit: int, offset: int
    ) -> list[Row]:
        return await self._rows(
            """
            SELECT * FROM v_yield_spread
             WHERE name = :name AND day >= :start AND day < :end
             ORDER BY day
             LIMIT :limit OFFSET :offset
            """,
            name=name,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )

    async def yield_curves(self, *, curve: str | None, limit: int, offset: int) -> list[Row]:
        query = _Query()
        if curve is not None:
            query.where("curve = :curve", curve=curve)
        return await self._rows(
            f"""
            SELECT * FROM v_yield_curve
             WHERE {query.sql()}
             ORDER BY curve, provider, maturity_months
             LIMIT :limit OFFSET :offset
            """,
            query,
            limit=limit,
            offset=offset,
        )

    # --- Watchlists and groups --------------------------------------------------------

    async def lists(self, *, kind: str, name: str | None, limit: int, offset: int) -> list[Row]:
        """Enabled lists of one kind, by name."""
        query = _Query()
        query.where("kind = :kind AND enabled", kind=kind)
        if name is not None:
            query.where("name = :name", name=name)
        return await self._rows(
            f"""
            SELECT name, kind, title, description FROM instrument_list
             WHERE {query.sql()}
             ORDER BY name
             LIMIT :limit OFFSET :offset
            """,
            query,
            limit=limit,
            offset=offset,
        )

    async def list_members(self, names: Sequence[str]) -> list[Row]:
        query = _Query()
        query.array("names", names, _TEXTS)
        return await self._rows(
            """
            SELECT list, position, instrument_id, symbol, instrument_name AS name, asset_type
              FROM v_instrument_list
             WHERE list = ANY (:names)
             ORDER BY list, position
            """,
            query,
        )

    async def list_instrument_ids(self, name: str) -> list[uuid.UUID] | None:
        """A list's members, in order; None if there is no such enabled list."""
        rows = await self._rows(
            """
            SELECT m.instrument_id FROM instrument_list l
              LEFT JOIN instrument_list_member m ON m.list_id = l.id
             WHERE l.name = :name AND l.enabled
             ORDER BY m.position
            """,
            name=name,
        )
        if not rows:
            return None
        return [row["instrument_id"] for row in rows if row["instrument_id"] is not None]

    async def group_performance(self, names: Sequence[str]) -> list[Row]:
        query = _Query()
        query.array("names", names, _TEXTS)
        return await self._rows(
            """
            SELECT "group", horizon, performance, members_with_value, members
              FROM v_group_performance
             WHERE "group" = ANY (:names)
            """,
            query,
        )

    # --- Portfolios --------------------------------------------------------------------

    async def portfolios(self, *, name: str | None, limit: int, offset: int) -> list[Row]:
        """Portfolios with their totals in the base currency."""
        query = _Query()
        if name is not None:
            query.where("pf.name = :name", name=name)
        return await self._rows(
            f"""
            SELECT pf.name, pf.base_currency, pf.loaded_at,
                   count(h.holding_id) AS holdings,
                   count(h.value) AS valued_holdings,
                   sum(h.value) AS value,
                   sum(h.cost) AS cost
              FROM portfolio pf
              LEFT JOIN v_portfolio_holding h ON h.portfolio = pf.name
             WHERE {query.sql()}
             GROUP BY pf.name, pf.base_currency, pf.loaded_at
             ORDER BY pf.name
             LIMIT :limit OFFSET :offset
            """,
            query,
            limit=limit,
            offset=offset,
        )

    async def portfolio_holdings(self, name: str) -> list[Row]:
        return await self._rows(
            """
            SELECT * FROM v_portfolio_holding
             WHERE portfolio = :name
             ORDER BY value DESC NULLS LAST, symbol, provider
            """,
            name=name,
        )

    async def portfolio_history(
        self, *, name: str, start: datetime, end: datetime, limit: int, offset: int
    ) -> list[Row]:
        return await self._rows(
            """
            SELECT * FROM portfolio_value_history(:name, :start, :end)
             ORDER BY day
             LIMIT :limit OFFSET :offset
            """,
            name=name,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
