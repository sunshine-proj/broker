"""Analytics, watchlists, groups, yield spreads and portfolios (PRD FR-014 to FR-016).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-17

- daily_observation: a continuous aggregate of each series' daily open, high,
  low and last value, refreshed every 30 minutes and completed in real time.
- v_daily_close: one close per instrument, provider and UTC day; the daily
  candle where there is one, else the day's last observation.
- Calculations as views and functions, so the API and Grafana share them.
- Tables for watchlists and groups and yield spreads (from the seed file) and
  portfolios (from the private portfolio file).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)
PRICE_LIKE = "('price', 'nav', 'rate', 'yield', 'index_level')"

DAILY_OBSERVATION = f"""
    CREATE MATERIALIZED VIEW daily_observation
    WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
    SELECT instrument_id,
           provider_id,
           observation_type,
           time_bucket(INTERVAL '1 day', observed_at) AS day,
           first(value, observed_at) AS open,
           max(value) AS high,
           min(value) AS low,
           last(value, observed_at) AS close,
           count(*) AS observations,
           last(currency, observed_at) AS currency,
           last(unit, observed_at) AS unit
      FROM market_observation
     WHERE observation_type IN {PRICE_LIKE}
     GROUP BY instrument_id, provider_id, observation_type,
              time_bucket(INTERVAL '1 day', observed_at)
    WITH NO DATA
"""

# Functions, by signature, in creation order. Those called once per row are
# PL/pgSQL, whose query plans are cached; SQL functions that are not inlined
# are planned again on every call.
FUNCTIONS = {
    # The observation type a daily candle closes: an instrument's main value.
    "primary_observation_type(text)": """
        CREATE FUNCTION primary_observation_type(p_asset_type text)
        RETURNS text LANGUAGE sql IMMUTABLE AS $$
            SELECT CASE p_asset_type
                       WHEN 'forex' THEN 'rate'
                       WHEN 'interest_rate' THEN 'rate'
                       WHEN 'government_bond_yield' THEN 'yield'
                       WHEN 'index' THEN 'index_level'
                       ELSE 'price' END
        $$
    """,
    # The newest daily close of one series at or before `at`, less than
    # `max_age` older: the daily candle's close if there is one, else the day's last
    # observation. Candles only close an instrument's main observation type.
    "daily_close_at(uuid, uuid, text, timestamptz, interval)": """
        CREATE FUNCTION daily_close_at(
            p_instrument uuid, p_provider uuid, p_type text, p_at timestamptz,
            p_max_age interval)
        RETURNS TABLE (day timestamptz, close numeric, currency text)
        LANGUAGE plpgsql STABLE AS $$
        BEGIN
            RETURN QUERY
            SELECT d.day, d.close, d.currency FROM (
                (SELECT c.timestamp AS day, c.close, c.currency::text AS currency, 0 AS rank
                   FROM ohlcv c
                   JOIN instrument i ON i.id = c.instrument_id
                  WHERE c.instrument_id = p_instrument AND c.provider_id = p_provider
                    AND c.interval = '1day'
                    AND primary_observation_type(i.asset_type) = p_type
                    AND c.timestamp <= p_at AND c.timestamp > p_at - p_max_age
                  ORDER BY c.timestamp DESC LIMIT 1)
                UNION ALL
                (SELECT o.day, o.close, o.currency::text, 1
                   FROM daily_observation o
                  WHERE o.instrument_id = p_instrument AND o.provider_id = p_provider
                    AND o.observation_type = p_type
                    AND o.day <= p_at AND o.day > p_at - p_max_age
                  ORDER BY o.day DESC LIMIT 1)
            ) d
            ORDER BY d.day DESC, d.rank
            LIMIT 1;
        END
        $$
    """,
    # The first day a series has a daily close.
    "series_start(uuid, uuid, text)": """
        CREATE FUNCTION series_start(p_instrument uuid, p_provider uuid, p_type text)
        RETURNS timestamptz LANGUAGE sql STABLE AS $$
            SELECT least(
                (SELECT min(c.timestamp) FROM ohlcv c
                   JOIN instrument i ON i.id = c.instrument_id
                  WHERE c.instrument_id = p_instrument AND c.provider_id = p_provider
                    AND c.interval = '1day'
                    AND primary_observation_type(i.asset_type) = p_type),
                (SELECT min(o.day) FROM daily_observation o
                  WHERE o.instrument_id = p_instrument AND o.provider_id = p_provider
                    AND o.observation_type = p_type))
        $$
    """,
    # A direct rate between two currencies on a day, from any pair instrument
    # quoting them (or its inverse), at most 4 days older; the ECB first.
    "fx_direct(text, text, timestamptz)": """
        CREATE FUNCTION fx_direct(p_from text, p_to text, p_at timestamptz)
        RETURNS numeric LANGUAGE plpgsql STABLE AS $$
        DECLARE
            result numeric;
        BEGIN
            SELECT CASE WHEN c.inverse THEN 1 / q.close ELSE q.close END INTO result
              FROM (SELECT m.instrument_id, m.provider_id, p.name AS provider,
                           primary_observation_type(i.asset_type) AS observation_type,
                           (i.metadata->>'base' = p_to) AS inverse
                      FROM instrument i
                      JOIN instrument_provider_mapping m ON m.instrument_id = i.id
                      JOIN provider p ON p.id = m.provider_id
                     WHERE i.asset_type IN ('forex', 'crypto')
                       AND ((i.metadata->>'base' = p_from AND i.metadata->>'quote' = p_to)
                         OR (i.metadata->>'base' = p_to AND i.metadata->>'quote' = p_from))
                   ) c
             CROSS JOIN LATERAL daily_close_at(
                   c.instrument_id, c.provider_id, c.observation_type, p_at,
                   INTERVAL '5 days') q
             WHERE q.close <> 0
             ORDER BY (c.provider = 'ecb') DESC, q.day DESC, c.provider
             LIMIT 1;
            RETURN result;
        END
        $$
    """,
    # Units of p_to per unit of p_from on a day: direct, or across EUR or USD.
    "fx_rate(text, text, timestamptz)": """
        CREATE FUNCTION fx_rate(p_from text, p_to text, p_at timestamptz)
        RETURNS numeric LANGUAGE plpgsql STABLE AS $$
        DECLARE
            rate numeric;
            via text;
        BEGIN
            IF p_from IS NULL OR p_to IS NULL OR p_at IS NULL THEN
                RETURN NULL;
            END IF;
            IF p_from = p_to THEN
                RETURN 1;
            END IF;
            rate := fx_direct(p_from, p_to, p_at);
            IF rate IS NOT NULL THEN
                RETURN rate;
            END IF;
            FOREACH via IN ARRAY ARRAY['EUR', 'USD'] LOOP
                IF via <> p_from AND via <> p_to THEN
                    rate := fx_direct(p_from, via, p_at) * fx_direct(via, p_to, p_at);
                    IF rate IS NOT NULL THEN
                        RETURN rate;
                    END IF;
                END IF;
            END LOOP;
            RETURN NULL;
        END
        $$
    """,
}

VIEWS = {
    # One close per instrument, provider, observation type and UTC day: the
    # daily candle where there is one, else the day's last observation.
    "v_daily_close": """
        SELECT i.id AS instrument_id,
               i.symbol,
               i.name AS instrument_name,
               i.asset_type,
               i.metadata AS instrument_metadata,
               p.id AS provider_id,
               p.name AS provider,
               d.observation_type,
               d.day,
               d.close,
               coalesce(d.currency, i.currency) AS currency,
               d.unit,
               d.source
          FROM (SELECT c.instrument_id, c.provider_id,
                       primary_observation_type(ci.asset_type) AS observation_type,
                       c.timestamp AS day, c.close, c.currency::text AS currency,
                       CASE WHEN ci.asset_type IN ('government_bond_yield', 'interest_rate')
                            THEN 'percent'
                            WHEN ci.asset_type = 'index' THEN 'points'
                            ELSE 'currency' END AS unit,
                       'candle' AS source
                  FROM ohlcv c
                  JOIN instrument ci ON ci.id = c.instrument_id
                 WHERE c.interval = '1day'
                UNION ALL
                SELECT o.instrument_id, o.provider_id, o.observation_type, o.day, o.close,
                       o.currency::text, o.unit::text, 'observations'
                  FROM daily_observation o
                 WHERE NOT EXISTS (
                       SELECT 1 FROM ohlcv c
                         JOIN instrument ci ON ci.id = c.instrument_id
                        WHERE c.instrument_id = o.instrument_id
                          AND c.provider_id = o.provider_id
                          AND c.interval = '1day'
                          AND c.timestamp = o.day
                          AND primary_observation_type(ci.asset_type) = o.observation_type)
               ) d
          JOIN instrument i ON i.id = d.instrument_id
          JOIN provider p ON p.id = d.provider_id
    """,
    # Daily returns, moving averages and volatility. Values in percent move in
    # points: their return is empty and their volatility is of daily changes.
    "v_daily_metrics": """
        SELECT s.instrument_id, s.symbol, s.instrument_name, s.asset_type,
               s.instrument_metadata, s.provider_id, s.provider, s.observation_type,
               s.day, s.close, s.currency, s.unit, s.source, s.previous_close,
               s.close - s.previous_close AS daily_change,
               CASE WHEN s.unit <> 'percent' AND s.previous_close <> 0
                    THEN s.close / s.previous_close - 1 END AS daily_return,
               CASE WHEN count(*) OVER w20 = 20 THEN avg(s.close) OVER w20 END AS sma_20,
               CASE WHEN count(*) OVER w50 = 50 THEN avg(s.close) OVER w50 END AS sma_50,
               CASE WHEN count(*) OVER w200 = 200 THEN avg(s.close) OVER w200 END AS sma_200,
               CASE WHEN count(s.step) OVER w20 = 20
                    THEN stddev_samp(s.step) OVER w20 * sqrt(s.periods_per_year) END
                   AS volatility_20d,
               CASE WHEN count(s.step) OVER w60 = 60
                    THEN stddev_samp(s.step) OVER w60 * sqrt(s.periods_per_year) END
                   AS volatility_60d
          FROM (SELECT d.*,
                       lag(d.close) OVER w AS previous_close,
                       CASE WHEN d.unit = 'percent' THEN d.close - lag(d.close) OVER w
                            WHEN lag(d.close) OVER w > 0 AND d.close > 0
                            THEN ln(d.close / lag(d.close) OVER w) END AS step,
                       CASE WHEN d.asset_type = 'crypto' THEN 365::numeric
                            ELSE 252::numeric END AS periods_per_year
                  FROM v_daily_close d
                WINDOW w AS (PARTITION BY d.instrument_id, d.provider_id, d.observation_type
                             ORDER BY d.day)) s
        WINDOW w20 AS (PARTITION BY s.instrument_id, s.provider_id, s.observation_type
                       ORDER BY s.day ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
               w50 AS (PARTITION BY s.instrument_id, s.provider_id, s.observation_type
                       ORDER BY s.day ROWS BETWEEN 49 PRECEDING AND CURRENT ROW),
               w60 AS (PARTITION BY s.instrument_id, s.provider_id, s.observation_type
                       ORDER BY s.day ROWS BETWEEN 59 PRECEDING AND CURRENT ROW),
               w200 AS (PARTITION BY s.instrument_id, s.provider_id, s.observation_type
                        ORDER BY s.day ROWS BETWEEN 199 PRECEDING AND CURRENT ROW)
    """,
    # Weekly (from Monday) and monthly returns, from each period's last close.
    "v_period_return": """
        SELECT x.instrument_id, x.symbol, x.asset_type, x.provider_id, x.provider,
               x.observation_type, x.unit, x.currency, x.period, x.period_start,
               x.day AS last_day, x.close, x.previous_close,
               x.close - x.previous_close AS change,
               CASE WHEN x.unit <> 'percent' AND x.previous_close <> 0
                    THEN x.close / x.previous_close - 1 END AS return
          FROM (SELECT l.*,
                       lag(l.close) OVER (PARTITION BY l.instrument_id, l.provider_id,
                                                       l.observation_type, l.period
                                          ORDER BY l.period_start) AS previous_close
                  FROM (SELECT DISTINCT ON (d.instrument_id, d.provider_id, d.observation_type,
                                            p.period, date_trunc(p.period, d.day, 'UTC'))
                               d.instrument_id, d.symbol, d.asset_type, d.provider_id,
                               d.provider, d.observation_type, d.unit, d.currency, d.day,
                               d.close, p.period,
                               date_trunc(p.period, d.day, 'UTC') AS period_start
                          FROM v_daily_close d
                         CROSS JOIN (VALUES ('week'), ('month')) AS p(period)
                         ORDER BY d.instrument_id, d.provider_id, d.observation_type,
                                  p.period, date_trunc(p.period, d.day, 'UTC'), d.day DESC
                       ) l
               ) x
    """,
    # The latest close of each series with its returns over standard horizons,
    # against the last close at or before each date. In points for percent units.
    "v_performance": """
        SELECT l.instrument_id, l.symbol, l.instrument_name, l.asset_type,
               l.instrument_metadata, l.provider_id, l.provider, l.observation_type,
               l.unit, l.currency, l.day, l.close, l.daily_change, l.daily_return,
               l.sma_20, l.sma_50, l.sma_200, l.volatility_20d, l.volatility_60d,
               h.horizon, h.since, h.past_close,
               CASE WHEN l.unit = 'percent' THEN l.close - h.past_close
                    WHEN h.past_close <> 0 THEN l.close / h.past_close - 1 END AS performance
          FROM (SELECT DISTINCT ON (instrument_id, provider_id, observation_type) *
                  FROM v_daily_metrics
                 ORDER BY instrument_id, provider_id, observation_type, day DESC) l
         CROSS JOIN LATERAL (
               SELECT v.horizon, q.day AS since, q.close AS past_close
                 FROM (VALUES ('1w', l.day - INTERVAL '7 days'),
                              ('1m', l.day - INTERVAL '1 month'),
                              ('3m', l.day - INTERVAL '3 months'),
                              ('ytd', date_trunc('year', l.day, 'UTC') - INTERVAL '1 second'),
                              ('1y', l.day - INTERVAL '1 year')) AS v(horizon, at)
                 LEFT JOIN LATERAL daily_close_at(
                       l.instrument_id, l.provider_id, l.observation_type, v.at,
                       INTERVAL '10 days') q ON true
               ) h
    """,
    # Watchlist and group members, in order.
    "v_instrument_list": """
        SELECT l.kind, l.name AS list, l.title, l.description, m.position,
               i.id AS instrument_id, i.symbol, i.name AS instrument_name, i.asset_type
          FROM instrument_list l
          JOIN instrument_list_member m ON m.list_id = l.id
          JOIN instrument i ON i.id = m.instrument_id
         WHERE l.enabled
    """,
    # Each group's equal-weighted performance per horizon. Each member counts
    # once, with its main series from the provider with the most daily closes.
    # Rates and yields change in points, not returns, and are not averaged.
    "v_group_performance": """
        SELECT g.list AS "group", g.title, h.horizon,
               avg(f.performance) FILTER (WHERE f.unit <> 'percent') AS performance,
               count(f.performance) FILTER (WHERE f.unit <> 'percent') AS members_with_value,
               count(*) AS members
          FROM (SELECT DISTINCT ON (m.list, m.instrument_id)
                       m.list, m.title, m.instrument_id, s.provider_id
                  FROM v_instrument_list m
                  LEFT JOIN LATERAL (
                       SELECT d.provider_id, count(*) AS closes
                         FROM v_daily_close d
                        WHERE d.instrument_id = m.instrument_id
                          AND d.observation_type = primary_observation_type(m.asset_type)
                        GROUP BY d.provider_id) s ON true
                 WHERE m.kind = 'group'
                 ORDER BY m.list, m.instrument_id, s.closes DESC NULLS LAST, s.provider_id) g
         CROSS JOIN (VALUES ('1w'), ('1m'), ('3m'), ('ytd'), ('1y')) AS h(horizon)
          LEFT JOIN v_performance f
            ON f.instrument_id = g.instrument_id AND f.provider_id = g.provider_id
           AND f.observation_type = primary_observation_type(f.asset_type)
           AND f.horizon = h.horizon
         GROUP BY g.list, g.title, h.horizon
    """,
    # Spreads on the days both series have a close.
    "v_yield_spread": """
        SELECT s.name, s.title, a.day,
               a.symbol AS long_symbol, a.provider AS long_provider, a.close AS long_value,
               b.symbol AS short_symbol, b.provider AS short_provider, b.close AS short_value,
               a.close - b.close AS spread,
               (a.close - b.close) * 100 AS spread_bp
          FROM yield_spread s
          JOIN v_daily_close a
            ON a.instrument_id = s.long_instrument_id AND a.provider_id = s.long_provider_id
           AND a.observation_type = primary_observation_type(a.asset_type)
          JOIN v_daily_close b
            ON b.instrument_id = s.short_instrument_id AND b.provider_id = s.short_provider_id
           AND b.observation_type = primary_observation_type(b.asset_type)
           AND b.day = a.day
         WHERE s.enabled
    """,
    # Each curve's yields by maturity: latest, and a week, a month and a year earlier.
    "v_yield_curve": """
        SELECT i.metadata->>'curve' AS curve,
               i.metadata->>'maturity' AS maturity,
               CASE right(i.metadata->>'maturity', 1)
                    WHEN 'M' THEN left(i.metadata->>'maturity', -1)::int
                    ELSE left(i.metadata->>'maturity', -1)::int * 12 END AS maturity_months,
               i.symbol, p.name AS provider, latest.day,
               latest.close AS latest,
               w.close AS week_ago, m1.close AS month_ago, y1.close AS year_ago
          FROM instrument i
          JOIN instrument_provider_mapping mp ON mp.instrument_id = i.id
          JOIN provider p ON p.id = mp.provider_id
         CROSS JOIN LATERAL daily_close_at(
               i.id, p.id, 'yield', now(), INTERVAL '30 days') latest
          LEFT JOIN LATERAL daily_close_at(
               i.id, p.id, 'yield', latest.day - INTERVAL '7 days', INTERVAL '10 days') w
            ON true
          LEFT JOIN LATERAL daily_close_at(
               i.id, p.id, 'yield', latest.day - INTERVAL '1 month', INTERVAL '10 days') m1
            ON true
          LEFT JOIN LATERAL daily_close_at(
               i.id, p.id, 'yield', latest.day - INTERVAL '1 year', INTERVAL '10 days') y1
            ON true
         WHERE i.asset_type = 'government_bond_yield' AND i.enabled
           AND i.metadata ? 'curve' AND i.metadata->>'maturity' ~ '^[0-9]+[MY]$'
    """,
    # Each holding valued now, in its own and the portfolio's base currency.
    # Its cost is converted on its acquisition date, or on its first valued day.
    "v_portfolio_holding": """
        SELECT x.*,
               x.value - x.cost AS profit,
               CASE WHEN x.cost <> 0 THEN x.value / x.cost - 1 END AS profit_percent,
               x.value / nullif(sum(x.value) OVER (PARTITION BY x.portfolio), 0) AS weight
          FROM (SELECT pf.name AS portfolio, pf.base_currency, h.id AS holding_id,
                       i.id AS instrument_id, i.symbol, i.name AS instrument_name,
                       i.asset_type, p.name AS provider,
                       h.quantity, h.cost_basis, h.cost_currency, h.acquired_on, h.note,
                       s.since AS valued_since,
                       q.day AS price_day, q.close AS price, q.currency AS price_currency,
                       h.quantity * q.close AS value_local,
                       h.quantity * q.close * fx_rate(q.currency, pf.base_currency, q.day)
                           AS value,
                       h.cost_basis * fx_rate(h.cost_currency, pf.base_currency, s.since)
                           AS cost
                  FROM portfolio_holding h
                  JOIN portfolio pf ON pf.id = h.portfolio_id
                  JOIN instrument i ON i.id = h.instrument_id
                  JOIN provider p ON p.id = h.provider_id
                 CROSS JOIN LATERAL (
                       SELECT coalesce(h.acquired_on::timestamp AT TIME ZONE 'UTC',
                                       series_start(h.instrument_id, h.provider_id,
                                                    primary_observation_type(i.asset_type)))
                                  AS since) s
                  LEFT JOIN LATERAL daily_close_at(
                       h.instrument_id, h.provider_id, primary_observation_type(i.asset_type),
                       now(), INTERVAL '8 days') q ON true
               ) x
    """,
}

# Functions that read the views, created after them.
LATE_FUNCTIONS = {
    # Each series of the given instruments rebased to 100 on its first close at
    # or after p_from. Values in percent are not rebased: they move in points.
    "relative_performance(uuid[], timestamptz, timestamptz)": """
        CREATE FUNCTION relative_performance(
            p_instruments uuid[], p_from timestamptz, p_to timestamptz)
        RETURNS TABLE (instrument_id uuid, symbol text, provider text,
                       observation_type text, day timestamptz, close numeric,
                       base_close numeric, rebased numeric)
        LANGUAGE sql STABLE AS $$
            SELECT r.instrument_id, r.symbol, r.provider, r.observation_type, r.day, r.close,
                   r.base_close,
                   CASE WHEN r.unit <> 'percent' AND r.base_close <> 0
                        THEN r.close / r.base_close * 100 END
              FROM (SELECT d.instrument_id, d.symbol::text, d.provider::text,
                           d.observation_type, d.day, d.close, d.unit,
                           first_value(d.close) OVER (
                               PARTITION BY d.instrument_id, d.provider_id, d.observation_type
                               ORDER BY d.day) AS base_close
                      FROM v_daily_close d
                     WHERE d.instrument_id = ANY (p_instruments)
                       AND d.day >= p_from AND d.day < p_to) r
             ORDER BY r.day, r.symbol, r.provider, r.observation_type
        $$
    """,
    # A portfolio's value on every calendar day from p_from to p_to, in its base
    # currency. A holding counts from its acquisition date (or its first close),
    # at its newest close at most 7 days older. Set-based: each holding's closes
    # are carried forward, and each currency converted once per day.
    "portfolio_value_history(text, timestamptz, timestamptz)": """
        CREATE FUNCTION portfolio_value_history(
            p_portfolio text, p_from timestamptz, p_to timestamptz)
        RETURNS TABLE (day timestamptz, value numeric, cost numeric, holdings bigint,
                       valued_holdings bigint)
        LANGUAGE sql STABLE AS $$
            WITH bounds AS (
                SELECT date_trunc('day', p_from, 'UTC') AS first_day,
                       date_trunc('day', least(p_to - INTERVAL '1 microsecond', now()), 'UTC')
                           AS last_day),
            held AS MATERIALIZED (
                SELECT h.id, h.instrument_id, h.provider_id, h.quantity, pf.base_currency,
                       primary_observation_type(i.asset_type) AS observation_type, s.since,
                       h.cost_basis * fx_rate(h.cost_currency, pf.base_currency, s.since)
                           AS cost
                  FROM portfolio pf
                  JOIN portfolio_holding h ON h.portfolio_id = pf.id
                  JOIN instrument i ON i.id = h.instrument_id
                 CROSS JOIN LATERAL (
                       SELECT coalesce(h.acquired_on::timestamp AT TIME ZONE 'UTC',
                                       series_start(h.instrument_id, h.provider_id,
                                                    primary_observation_type(i.asset_type)))
                                  AS since) s
                 WHERE pf.name = p_portfolio AND s.since IS NOT NULL),
            closes AS MATERIALIZED (
                SELECT held.id, c.day, c.close, c.currency
                  FROM held, bounds b, v_daily_close c
                 WHERE c.instrument_id = held.instrument_id
                   AND c.provider_id = held.provider_id
                   AND c.observation_type = held.observation_type
                   AND c.day > greatest(b.first_day, date_trunc('day', held.since, 'UTC'))
                               - INTERVAL '8 days'
                   AND c.day <= b.last_day),
            grid AS (
                SELECT held.id, d.day, c.day AS price_day, c.close, c.currency,
                       count(c.day) OVER (PARTITION BY held.id ORDER BY d.day) AS run
                  FROM held CROSS JOIN bounds b
                 CROSS JOIN LATERAL generate_series(
                       greatest(b.first_day, date_trunc('day', held.since, 'UTC'))
                           - INTERVAL '7 days',
                       b.last_day, INTERVAL '1 day') AS d(day)
                  LEFT JOIN closes c ON c.id = held.id AND c.day = d.day),
            carried AS (
                SELECT g.id, g.day,
                       first_value(g.price_day) OVER w AS price_day,
                       first_value(g.close) OVER w AS close,
                       first_value(g.currency) OVER w AS currency
                  FROM grid g
                WINDOW w AS (PARTITION BY g.id, g.run ORDER BY g.day)),
            priced AS (
                SELECT held.id, c.day, held.cost, held.base_currency,
                       CASE WHEN c.price_day > c.day - INTERVAL '8 days'
                            THEN held.quantity * c.close END AS value_local,
                       CASE WHEN c.price_day > c.day - INTERVAL '8 days'
                            THEN c.currency END AS currency,
                       c.price_day
                  FROM carried c
                  JOIN held ON held.id = c.id
                 CROSS JOIN bounds b
                 WHERE c.day >= b.first_day AND c.day >= held.since),
            rates AS (
                SELECT r.currency, r.base_currency, r.price_day,
                       fx_rate(r.currency, r.base_currency, r.price_day) AS rate
                  FROM (SELECT DISTINCT currency, base_currency, price_day
                          FROM priced WHERE currency IS NOT NULL) r)
            SELECT p.day, sum(p.value_local * r.rate), sum(p.cost), count(*),
                   count(p.value_local * r.rate)
              FROM priced p
              LEFT JOIN rates r
                ON r.currency = p.currency AND r.base_currency = p.base_currency
               AND r.price_day = p.price_day
             GROUP BY p.day
             ORDER BY p.day
        $$
    """,
}


def upgrade() -> None:
    op.create_table(
        "instrument_list",
        sa.Column("id", sa.Uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text),
        sa.Column("enabled", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_instrument_list"),
        sa.UniqueConstraint("name", name="uq_instrument_list_name"),
        sa.CheckConstraint("kind IN ('watchlist', 'group')", name=op.f("ck_instrument_list_kind")),
    )
    op.create_table(
        "instrument_list_member",
        sa.Column("list_id", sa.Uuid, nullable=False),
        sa.Column("instrument_id", sa.Uuid, nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.PrimaryKeyConstraint("list_id", "instrument_id", name="pk_instrument_list_member"),
        sa.ForeignKeyConstraint(
            ["list_id"],
            ["instrument_list.id"],
            name="fk_instrument_list_member_list_id_instrument_list",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name="fk_instrument_list_member_instrument_id_instrument",
        ),
    )
    op.create_table(
        "yield_spread",
        sa.Column("id", sa.Uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("long_instrument_id", sa.Uuid, nullable=False),
        sa.Column("long_provider_id", sa.Uuid, nullable=False),
        sa.Column("short_instrument_id", sa.Uuid, nullable=False),
        sa.Column("short_provider_id", sa.Uuid, nullable=False),
        sa.Column("enabled", sa.Boolean, server_default=sa.true(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_yield_spread"),
        sa.UniqueConstraint("name", name="uq_yield_spread_name"),
        *(
            sa.ForeignKeyConstraint(
                [f"{leg}_{column}_id"],
                [f"{table}.id"],
                name=f"fk_yield_spread_{leg}_{column}_id_{table}",
            )
            for leg in ("long", "short")
            for column, table in (("instrument", "instrument"), ("provider", "provider"))
        ),
    )
    op.create_table(
        "portfolio",
        sa.Column("id", sa.Uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("base_currency", sa.String(10), nullable=False),
        sa.Column("loaded_at", TIMESTAMPTZ, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_portfolio"),
        sa.UniqueConstraint("name", name="uq_portfolio_name"),
    )
    op.create_table(
        "portfolio_holding",
        sa.Column("id", sa.Uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("portfolio_id", sa.Uuid, nullable=False),
        sa.Column("instrument_id", sa.Uuid, nullable=False),
        sa.Column("provider_id", sa.Uuid, nullable=False),
        sa.Column("quantity", sa.Numeric, nullable=False),
        sa.Column("cost_basis", sa.Numeric, nullable=False),
        sa.Column("cost_currency", sa.String(10), nullable=False),
        sa.Column("acquired_on", sa.Date),
        sa.Column("note", sa.Text),
        sa.PrimaryKeyConstraint("id", name="pk_portfolio_holding"),
        sa.ForeignKeyConstraint(
            ["portfolio_id"],
            ["portfolio.id"],
            name="fk_portfolio_holding_portfolio_id_portfolio",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name="fk_portfolio_holding_instrument_id_instrument",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["provider.id"], name="fk_portfolio_holding_provider_id_provider"
        ),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_portfolio_holding_quantity")),
        sa.CheckConstraint("cost_basis >= 0", name=op.f("ck_portfolio_holding_cost_basis")),
    )

    op.execute(DAILY_OBSERVATION)
    for statement in FUNCTIONS.values():
        op.execute(statement)
    for name, query in VIEWS.items():
        op.execute(f"CREATE VIEW {name} AS {query}")
    for statement in LATE_FUNCTIONS.values():
        op.execute(statement)

    # Continuous aggregates are filled and scheduled outside a transaction.
    with op.get_context().autocommit_block():
        op.execute("CALL refresh_continuous_aggregate('daily_observation', NULL, NULL)")
        op.execute(
            "SELECT add_continuous_aggregate_policy('daily_observation', "
            "start_offset => NULL, end_offset => INTERVAL '1 hour', "
            "schedule_interval => INTERVAL '30 minutes')"
        )


def downgrade() -> None:
    for signature in reversed(LATE_FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    for name in reversed(VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {name}")
    for signature in reversed(FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS daily_observation")
    for table in (
        "portfolio_holding",
        "portfolio",
        "yield_spread",
        "instrument_list_member",
        "instrument_list",
    ):
        op.drop_table(table)
