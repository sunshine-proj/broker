"""Reporting views: the reusable SQL behind the Grafana dashboards.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-16

The views join names onto the stored rows and compute "latest value" and
"current value of a period" once, so dashboard queries stay short and the
rules live in one place. A new observation type must also be added to
v_latest_observation.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OBSERVATION_TYPES = (
    "price",
    "nav",
    "rate",
    "yield",
    "index_level",
    "market_cap",
    "volume",
    "circulating_supply",
    "total_supply",
    "dominance",
)

VIEWS = {
    # Every market observation, with instrument and provider names.
    "v_observation": """
        SELECT o.instrument_id,
               i.symbol,
               i.name AS instrument_name,
               i.asset_type,
               i.metadata AS instrument_metadata,
               p.name AS provider,
               o.observation_type,
               o.observed_at,
               o.ingested_at,
               o.value,
               o.currency,
               o.unit,
               o.freshness,
               o.quote_kind,
               o.quality_status
          FROM market_observation o
          JOIN instrument i ON i.id = o.instrument_id
          JOIN provider p ON p.id = o.provider_id
    """,
    # The newest observation of each instrument, provider and observation type.
    # Each series is looked up through its index, newest first.
    "v_latest_observation": f"""
        SELECT i.id AS instrument_id,
               i.symbol,
               i.name AS instrument_name,
               i.asset_type,
               i.metadata AS instrument_metadata,
               p.name AS provider,
               o.observation_type,
               o.observed_at,
               o.ingested_at,
               o.value,
               o.currency,
               o.unit,
               o.freshness,
               o.quote_kind,
               o.quality_status,
               EXTRACT(EPOCH FROM now() - o.observed_at) AS age_seconds
          FROM instrument_provider_mapping m
          JOIN instrument i ON i.id = m.instrument_id
          JOIN provider p ON p.id = m.provider_id
         CROSS JOIN (VALUES {", ".join(f"('{t}')" for t in OBSERVATION_TYPES)})
               AS wanted(observation_type)
         CROSS JOIN LATERAL (
               SELECT *
                 FROM market_observation mo
                WHERE mo.instrument_id = m.instrument_id
                  AND mo.provider_id = m.provider_id
                  AND mo.observation_type = wanted.observation_type
                ORDER BY mo.observed_at DESC
                LIMIT 1
               ) o
    """,
    # Candles, with instrument and provider names.
    "v_candle": """
        SELECT c.instrument_id,
               i.symbol,
               i.asset_type,
               p.name AS provider,
               c.interval,
               c.timestamp,
               c.open,
               c.high,
               c.low,
               c.close,
               c.volume,
               c.currency,
               c.ingested_at
          FROM ohlcv c
          JOIN instrument i ON i.id = c.instrument_id
          JOIN provider p ON p.id = c.provider_id
    """,
    # The current value of each indicator period (its latest revision), with
    # the number of values stored for the period.
    "v_indicator_value": """
        SELECT DISTINCT ON (e.indicator_id, e.observed_at)
               ind.code,
               ind.name,
               ind.country,
               ind.unit,
               ind.frequency,
               p.name AS provider,
               e.observed_at,
               e.ingested_at,
               e.value,
               count(*) OVER (PARTITION BY e.indicator_id, e.observed_at) AS revisions
          FROM economic_observation e
          JOIN economic_indicator ind ON ind.id = e.indicator_id
          JOIN provider p ON p.id = ind.provider_id
         ORDER BY e.indicator_id, e.observed_at, e.ingested_at DESC
    """,
    # How current each provider's data is, per asset type and observation type.
    "v_data_freshness": """
        SELECT provider,
               asset_type,
               observation_type,
               count(*) AS series,
               max(observed_at) AS last_observed_at,
               max(ingested_at) AS last_ingested_at,
               EXTRACT(EPOCH FROM now() - max(observed_at)) AS age_seconds
          FROM v_latest_observation
         GROUP BY provider, asset_type, observation_type
    """,
    # Providers with their current health. Configuration is left out.
    "v_provider_status": """
        SELECT p.name AS provider,
               p.enabled,
               coalesce(h.status, 'unknown') AS health,
               h.last_success_at,
               h.last_error_at,
               h.last_error,
               coalesce(h.consecutive_failures, 0) AS consecutive_failures
          FROM provider p
          LEFT JOIN provider_health h ON h.provider_id = p.id
    """,
    "v_job_status": """
        SELECT j.name AS job,
               p.name AS provider,
               j.kind,
               j.enabled,
               CASE WHEN j.daily_at IS NOT NULL
                    THEN 'daily ' || to_char(j.daily_at, 'HH24:MI') || ' UTC'
                    ELSE 'every ' || j.interval_minutes || ' min'
               END
               || CASE WHEN j.market_hours_only THEN ', trading hours' ELSE '' END AS schedule,
               j.last_started_at,
               j.last_finished_at,
               j.last_status,
               j.last_error,
               j.next_run_at
          FROM collection_job j
          JOIN provider p ON p.id = j.provider_id
    """,
    "v_execution": """
        SELECT e.id,
               e.job_name AS job,
               p.name AS provider,
               e.trigger,
               e.status,
               e.started_at,
               e.finished_at,
               EXTRACT(EPOCH FROM e.finished_at - e.started_at) AS duration_seconds,
               e.records_written,
               e.error
          FROM collector_execution e
          LEFT JOIN provider p ON p.id = e.provider_id
    """,
}


def upgrade() -> None:
    for name, query in VIEWS.items():
        op.execute(f"CREATE VIEW {name} AS {query}")


def downgrade() -> None:
    for name in reversed(VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {name}")
