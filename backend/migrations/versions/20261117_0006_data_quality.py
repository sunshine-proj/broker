"""Data quality: issues, quality and backfill jobs, and their reporting views.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_JOB_KINDS = ("quotes", "candles", "history", "indicators")
JOB_KINDS = (*OLD_JOB_KINDS, "quality", "backfill")
ISSUE_KINDS = ("stale", "gap", "conflict", "spike", "discrepancy")
ISSUE_STATUSES = ("open", "closed")
RESOLUTIONS = ("cleared", "filled", "no_data", "unrecoverable", "recorded")
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
INTERVALS = ("1min", "5min", "15min", "30min", "1h", "4h", "1day", "1week", "1month")

TIMESTAMPTZ = sa.DateTime(timezone=True)

# Same as in 0005, but a quality job has no provider.
V_JOB_STATUS = """
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
      {join} JOIN provider p ON p.id = j.provider_id
"""

VIEWS = {
    # Issues with instrument and provider names.
    "v_quality_issue": """
        SELECT q.id,
               q.kind,
               q.status,
               q.resolution,
               i.symbol,
               i.asset_type,
               p.name AS provider,
               o.name AS other_provider,
               q.observation_type,
               q.interval,
               q.range_start,
               q.range_end,
               q.message,
               q.details,
               q.detected_at,
               q.last_seen_at,
               q.closed_at
          FROM data_quality_issue q
          LEFT JOIN instrument i ON i.id = q.instrument_id
          LEFT JOIN provider p ON p.id = q.provider_id
          LEFT JOIN provider o ON o.id = q.other_provider_id
    """,
    # Two providers' values of one instrument at the same time. The older of
    # the two latest values is compared with the value the other provider
    # had then: its newest observation at or before that time, at most a day
    # earlier. A daily reference rate is so compared with the market rate at
    # its date, not with today's.
    "v_provider_discrepancy": """
        SELECT a.instrument_id,
               a.symbol,
               a.asset_type,
               a.observation_type,
               a.unit,
               a.provider,
               b.provider AS other_provider,
               CASE WHEN a.observed_at <= b.observed_at THEN a.value ELSE o.value END AS value,
               CASE WHEN a.observed_at <= b.observed_at THEN o.value ELSE b.value END
                   AS other_value,
               CASE WHEN a.observed_at <= b.observed_at THEN a.observed_at ELSE o.observed_at END
                   AS observed_at,
               CASE WHEN a.observed_at <= b.observed_at THEN o.observed_at ELSE b.observed_at END
                   AS other_observed_at,
               CASE WHEN a.observed_at <= b.observed_at THEN a.value - o.value
                    ELSE o.value - b.value END AS difference,
               CASE WHEN a.observed_at <= b.observed_at
                    THEN (a.value - o.value) / nullif(abs(o.value), 0) * 100
                    ELSE (o.value - b.value) / nullif(abs(b.value), 0) * 100 END
                   AS difference_percent
          FROM v_latest_observation a
          JOIN v_latest_observation b
            ON b.instrument_id = a.instrument_id
           AND b.observation_type = a.observation_type
           AND b.provider > a.provider
          JOIN provider pa ON pa.name = a.provider
          JOIN provider pb ON pb.name = b.provider
         CROSS JOIN LATERAL (
               SELECT mo.observed_at, mo.value
                 FROM market_observation mo
                WHERE mo.instrument_id = a.instrument_id
                  AND mo.observation_type = a.observation_type
                  -- The provider whose latest value is the newer one, at the older time.
                  AND mo.provider_id = CASE WHEN a.observed_at <= b.observed_at
                                            THEN pb.id ELSE pa.id END
                  AND mo.observed_at <= least(a.observed_at, b.observed_at)
                  AND mo.observed_at > least(a.observed_at, b.observed_at) - INTERVAL '1 day'
                ORDER BY mo.observed_at DESC
                LIMIT 1
               ) o
         WHERE a.observation_type IN ('price', 'nav', 'rate', 'yield', 'index_level')
    """,
    # How long values took from their own timestamp to storage, over the last
    # 24 hours of collection. Values observed more than 7 days before they
    # were stored (history imports) are left out.
    "v_collection_latency": """
        SELECT p.name AS provider,
               i.asset_type,
               o.observation_type,
               o.freshness,
               count(*) AS observations,
               percentile_cont(0.5) WITHIN GROUP (
                   ORDER BY EXTRACT(EPOCH FROM o.ingested_at - o.observed_at)) AS median_seconds,
               percentile_cont(0.95) WITHIN GROUP (
                   ORDER BY EXTRACT(EPOCH FROM o.ingested_at - o.observed_at)) AS p95_seconds,
               max(EXTRACT(EPOCH FROM o.ingested_at - o.observed_at)) AS max_seconds
          FROM market_observation o
          JOIN instrument i ON i.id = o.instrument_id
          JOIN provider p ON p.id = o.provider_id
         WHERE o.observed_at > now() - INTERVAL '8 days'
           AND o.ingested_at > now() - INTERVAL '24 hours'
           AND o.ingested_at - o.observed_at < INTERVAL '7 days'
         GROUP BY p.name, i.asset_type, o.observation_type, o.freshness
    """,
}


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_collection_job_kind"), "collection_job", type_="check")
    op.create_check_constraint(
        op.f("ck_collection_job_kind"), "collection_job", _in("kind", JOB_KINDS)
    )
    op.alter_column("collection_job", "provider_id", nullable=True)
    op.create_check_constraint(
        op.f("ck_collection_job_provider"),
        "collection_job",
        "(kind = 'quality') = (provider_id IS NULL)",
    )
    op.execute(f"CREATE OR REPLACE VIEW v_job_status AS {V_JOB_STATUS.format(join='LEFT')}")

    op.create_table(
        "data_quality_issue",
        sa.Column("id", sa.Uuid, server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("resolution", sa.String(32)),
        sa.Column("fingerprint", sa.String(400), nullable=False),
        sa.Column("instrument_id", sa.Uuid),
        sa.Column("provider_id", sa.Uuid),
        sa.Column("other_provider_id", sa.Uuid),
        sa.Column("observation_type", sa.String(32)),
        sa.Column("interval", sa.String(8)),
        sa.Column("range_start", TIMESTAMPTZ),
        sa.Column("range_end", TIMESTAMPTZ),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("details", JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("detected_at", TIMESTAMPTZ, nullable=False),
        sa.Column("last_seen_at", TIMESTAMPTZ, nullable=False),
        sa.Column("closed_at", TIMESTAMPTZ),
        sa.PrimaryKeyConstraint("id", name="pk_data_quality_issue"),
        sa.UniqueConstraint("fingerprint", name="uq_data_quality_issue_fingerprint"),
        sa.ForeignKeyConstraint(
            ["instrument_id"],
            ["instrument.id"],
            name="fk_data_quality_issue_instrument_id_instrument",
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"], ["provider.id"], name="fk_data_quality_issue_provider_id_provider"
        ),
        sa.ForeignKeyConstraint(
            ["other_provider_id"],
            ["provider.id"],
            name="fk_data_quality_issue_other_provider_id_provider",
        ),
        sa.CheckConstraint(_in("kind", ISSUE_KINDS), name=op.f("ck_data_quality_issue_kind")),
        sa.CheckConstraint(
            _in("status", ISSUE_STATUSES), name=op.f("ck_data_quality_issue_status")
        ),
        sa.CheckConstraint(
            _in("resolution", RESOLUTIONS), name=op.f("ck_data_quality_issue_resolution")
        ),
        sa.CheckConstraint(
            _in("observation_type", OBSERVATION_TYPES),
            name=op.f("ck_data_quality_issue_observation_type"),
        ),
        sa.CheckConstraint(_in("interval", INTERVALS), name=op.f("ck_data_quality_issue_interval")),
        # Open issues have no resolution; closed ones always do.
        sa.CheckConstraint(
            "(status = 'open') = (resolution IS NULL)", name=op.f("ck_data_quality_issue_closed")
        ),
    )
    op.create_index("ix_data_quality_issue_status_kind", "data_quality_issue", ["status", "kind"])
    op.create_index("ix_data_quality_issue_detected_at", "data_quality_issue", ["detected_at"])

    for name, query in VIEWS.items():
        op.execute(f"CREATE VIEW {name} AS {query}")


def downgrade() -> None:
    for name in reversed(VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {name}")
    op.drop_table("data_quality_issue")
    op.execute("DELETE FROM collection_job WHERE kind IN ('quality', 'backfill')")
    op.execute(f"CREATE OR REPLACE VIEW v_job_status AS {V_JOB_STATUS.format(join='')}")
    op.drop_constraint(op.f("ck_collection_job_provider"), "collection_job", type_="check")
    op.alter_column("collection_job", "provider_id", nullable=False)
    op.drop_constraint(op.f("ck_collection_job_kind"), "collection_job", type_="check")
    op.create_check_constraint(
        op.f("ck_collection_job_kind"), "collection_job", _in("kind", OLD_JOB_KINDS)
    )
